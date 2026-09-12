# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Project run status into mover screens and shared measurement status fields."""

from __future__ import annotations

import logging
from typing import Any, Mapping

from ..json_fields import finite_float as _finite
from ..log_event import log_event
from .frequency_display import prepare_frequency_curve
from .crossover_v2.durable_state import FINDING_HOUSEHOLD_REFS_KEY
from .candidate_trials import tuning_trial_matches_candidate
from .capture_status import CAPTURE_COMPLETE, CAPTURE_FAILED, SESSION_ENDED_STATUSES
from .crossover_v2.journey import (
    CAPTURE_PHASES,
    PHASE_APPLYING,
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_ENTRY_BASELINE,
    PHASE_CLOUD_VERIFY,
    PHASE_DONE,
    PHASE_LATERAL,
    PHASE_MEASURE,
    PHASE_REVIEW,
    PHASE_CLOSING,
    PHASE_VERIFY,
    PRE_CLOUD_CAPTURE_PHASES,
)
from .crossover_v2.spatial import _geometry_guidance_copy
from .crossover_v2.refusal_copy import (
    REASON_REGISTRY,
    ReasonSpec,
    reason_message,
)
from .crossover_v2_flow import (
    CLOUD_CLOSE_RUNNING,
)
from .crossover_v2.refusal_copy import REASON_VOLUME_UNRESOLVED

logger = logging.getLogger(__name__)

CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION = 17

_STEP_IDS = (
    "speaker_setup",
    "microphone_check",
    "measure",
    "verify",
)
_STEP_LABELS = {
    "speaker_setup": "Protected speaker setup",
    "microphone_check": "Microphone check",
    "measure": "Measure",
    "verify": "Verify",
}

_PHASE_STEP = {
    PHASE_CHECK: "microphone_check",
    PHASE_MEASURE: "measure",
    PHASE_CLOUD_MEASURE: "measure",
    PHASE_LATERAL: "measure",
    # #2291's entry baseline is the LAST thing stage 1 measures — still
    # measuring, nothing applied yet.
    PHASE_ENTRY_BASELINE: "measure",
    PHASE_CLOSING: "measure",
    PHASE_APPLYING: "measure",
    PHASE_REVIEW: "measure",
    PHASE_VERIFY: "verify",
    PHASE_CLOUD_VERIFY: "verify",
    PHASE_DONE: "verify",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _headroom_cost_payload(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """``{"db": float|None, "basis": str}`` — the correction's disclosed
    max-level cost, inseparable from the era that stamped it (#1808).
    ``basis`` is passed through rather than collapsed (the two peak eras
    disagree in the direction #2758 opened); anything else, including
    absence, is ``unknown``. ``db`` is ``None``, not ``0.0``, when missing
    or unusable — zero is a real common answer (cut-only corrections
    charge nothing).
    """
    from .linearization_fit import (
        HEADROOM_COST_BASIS_REALIZED_PEAK,
        HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN,
        HEADROOM_COST_BASIS_UNKNOWN,
    )

    basis = candidate.get("headroom_cost_basis")
    known = (
        HEADROOM_COST_BASIS_REALIZED_PEAK,
        HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN,
    )
    return {
        "db": _finite(candidate.get("headroom_cost_db")),
        "basis": str(basis) if basis in known else HEADROOM_COST_BASIS_UNKNOWN,
    }


def _verify_gate(status: Mapping[str, Any]) -> Mapping[str, Any]:
    """VERIFY's persisted gate record (``{"disclosure",
    "reflection_measured"}``). Empty when the state carries none — a
    legacy file, or a capture that could not be gated.
    """
    return _mapping(_mapping(_v2(status).get("verify")).get("gate"))


def _verify_gate_reflection_measured(status: Mapping[str, Any]) -> bool | None:
    """Whether VERIFY's gate actually found a reflection, or ``None``
    unknown — the fact the inconclusive copy branches on (#1974). ``None``
    is a third state, not a falsy second one.
    """
    measured = _verify_gate(status).get("reflection_measured")
    return measured if isinstance(measured, bool) else None


def _band_edges(value: Any) -> tuple[float, float] | None:
    """``(lo_hz, hi_hz)`` from a persisted two-element band pair, or
    ``None``. One spelling for the several band pairs a ``flatness`` block
    carries.
    """
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    lo, hi = _finite(value[0]), _finite(value[1])
    return None if lo is None or hi is None else (lo, hi)


_TILT_DIRECTION_FLOOR_DB = 0.005


def _attribution_lines(
    flatness: Mapping[str, Any], band_lo: float | None, band_hi: float | None,
) -> list[str]:
    """The two lines that stop the worst-band pointer from being read as
    "here is the peak to EQ" (#1857). A band inside the pooled reference
    that is uniformly off drags the shared zero and inflates every other
    band's number (a corpus session read +4.84 dB @ 1339.6 Hz against a
    woofer flat to +/-0.1 dB, because a ~5 dB dark tweeter pulled the
    frame down). Line one splits the pointer into where the band SITS vs.
    what the curve does INSIDE it; line two is the band-to-band step no
    reference-frame choice can move — trust it when the two disagree
    (ADR-0194). Disclosure only, decides nothing.
    """
    lines: list[str] = []
    level_db = _finite(flatness.get("max_band_level_deviation_db"))
    ripple_db = _finite(flatness.get("max_band_ripple_db"))
    if level_db is not None and ripple_db is not None:
        where = (
            f"the whole {band_lo:.0f}–{band_hi:.0f} Hz band"
            if band_lo is not None and band_hi is not None
            else "the whole band"
        )
        lines.append(
            f"of that, {level_db:+.2f} dB is where {where} sits; its own worst "
            f"excursion from that level is {ripple_db:+.2f} dB"
        )
    tilt = flatness.get("tilt")
    tilt = tilt if isinstance(tilt, Mapping) else {}
    step_db = _finite(tilt.get("step_db"))
    high = _band_edges(tilt.get("high_band_hz"))
    low = _band_edges(tilt.get("low_band_hz"))
    if tilt.get("evaluable") is True and step_db is not None:
        direction = (
            f": {high[0]:.0f}–{high[1]:.0f} Hz sits above "
            f"{low[0]:.0f}–{low[1]:.0f} Hz"
            if high is not None and low is not None
            and step_db >= _TILT_DIRECTION_FLOOR_DB
            else ""
        )
        lines.append(
            f"band levels differ by {step_db:.2f} dB, a reading no reference "
            f"choice moves{direction}"
        )
    return lines


def _flatness_lines_from_block(flatness: Mapping[str, Any]) -> list[str]:
    """The numeric flatness lines shared by both branches of the expert
    disclosure — max/avg deviation plus the excluded-bin count. Extracted
    so the post-apply claim (:func:`_flatness_details_lines`) and the
    before-tuning claim (:func:`_pre_apply_flatness_lines`) compute
    identical arithmetic. The line NAMES its reference frame (#1857): a
    block without the key keeps the previous unqualified wording rather
    than guessing at a frame. :func:`_attribution_lines` renders how much
    of the number is the frame.
    """
    lines: list[str] = []
    max_db = _finite(flatness.get("max_db"))
    max_hz = _finite(flatness.get("max_hz"))
    tolerance_db = _finite(flatness.get("tolerance_db"))
    band = _band_edges(flatness.get("max_band_hz"))
    band_lo, band_hi = band if band is not None else (None, None)
    ref = _band_edges(flatness.get("reference_band_hz"))
    ref_lo, ref_hi = ref if ref is not None else (None, None)
    if max_db is not None:
        where = f" at {max_hz:.0f} Hz" if max_hz is not None else ""
        against = (
            f" (spec {band_lo:.0f}–{band_hi:.0f} Hz, tolerance ±{tolerance_db:.1f} dB)"
            if band_lo is not None and band_hi is not None and tolerance_db is not None
            else ""
        )
        frame = (
            f"the {ref_lo:.0f}–{ref_hi:.0f} Hz reference mean"
            if ref_lo is not None and ref_hi is not None
            else "the spec reference"
        )
        lines.append(f"flatness {max_db:+.2f} dB from {frame}{where}{against}")
        lines.extend(_attribution_lines(flatness, band_lo, band_hi))
    rms_db = _finite(flatness.get("rms_db"))
    if rms_db is not None:
        lines.append(f"flatness average error {rms_db:.2f} dB across the spec bands")
    graded = flatness.get("n_bins")
    excluded = flatness.get("n_excluded")
    if isinstance(graded, int) and isinstance(excluded, int) and excluded > 0:
        # Bins, not "regions": an interval count would over-report, since
        # it spans the whole axis including frequencies no spec band grades.
        lines.append(
            f"{excluded} of {graded + excluded} spec-band bins excluded from "
            "grading (interference, or below the measurement's validity floor)"
        )
    return lines


def _per_band_flatness_lines(spec_bands: Any) -> list[str]:
    """Every graded band's OWN worst deviation, from the SAME reference the
    pointer line above names (#1857). ``_flatness_lines_from_block`` names
    ONE band, but a pooled reference lets an unrelated band's ripple read
    as the LARGER deviation (a shipped verdict read "+4.84 dB @ 1339.6 Hz"
    for the woofer band while the tweeter sat uniformly ~5 dB dark).
    Disclosure only, copied verbatim from ``spec_bands``; unevaluable
    bands are silently skipped.
    """
    if not isinstance(spec_bands, list):
        return []
    parts: list[str] = []
    for band in spec_bands:
        if not isinstance(band, Mapping):
            continue
        lo = _finite(band.get("f_lo_hz"))
        hi = _finite(band.get("f_hi_hz"))
        deviation_db = _finite(band.get("max_deviation_db"))
        tolerance_db = _finite(band.get("tolerance_db"))
        within_target = band.get("within_target")
        if (
            lo is None or hi is None or deviation_db is None
            or tolerance_db is None or not isinstance(within_target, bool)
        ):
            continue
        margin_db = abs(deviation_db) - tolerance_db
        compare = f"{margin_db:.1f} dB outside" if not within_target else "within"
        parts.append(
            f"{lo:.0f}–{hi:.0f} Hz {deviation_db:+.2f} dB "
            f"({compare} the ±{tolerance_db:.1f} dB target)"
        )
    if not parts:
        return []
    return ["every band from the same reference: " + ", ".join(parts)]


def _flatness_details_lines(status: Mapping[str, Any]) -> list[str]:
    block = _cloud_verify_block(status)
    if not block:
        return _pre_apply_flatness_lines(status)
    flatness = _mapping(block.get("flatness"))
    if not flatness:
        return _flatness_unavailable_line(block)
    if not flatness.get("evaluable"):
        # The gauge ran and could not measure — read ``SpecFlatness.passed`` with
        # ``evaluable``. Never render this as a pass or a fail. The carve-out
        # lines ride along because in this state they ARE the explanation.
        return [
            "flatness could not be measured — every spec band was excluded "
            "or out of range"
        ] + _carve_out_expert_lines(block)
    lines = _flatness_lines_from_block(flatness)
    lines.extend(_per_band_flatness_lines(block.get("spec_bands")))
    lines.extend(_carve_out_expert_lines(block))
    return lines


def _pre_apply_flatness_lines(status: Mapping[str, Any]) -> list[str]:
    block = _cloud_measure_block(status)
    flatness = _mapping(block.get("flatness"))
    if not flatness:
        return []
    if not flatness.get("evaluable"):
        # Same capitalized lead as the evaluable arm below, which read as a
        # fragment beside its sibling while lowercase.
        return [
            "Measured before tuning: flatness could not be measured — every "
            "spec band was excluded or out of range"
        ] + _carve_out_expert_lines(block)
    numeric = "; ".join(
        _flatness_lines_from_block(flatness)
        + _per_band_flatness_lines(block.get("spec_bands"))
    )
    line = f"Measured before tuning: {numeric}"
    if _mapping(_v2(status).get("verify")).get("outcome") == "pass":
        line += (
            ". The applied correction targets these; the result was confirmed "
            "at the mark only"
        )
    lines = [line]
    lines.extend(_carve_out_expert_lines(block))
    return lines


def _carve_out_expert_lines(block: Mapping[str, Any]) -> list[str]:
    """The carve-out τ/r lines (PR-6b, owner decision 1). The expert layer:
    the line above says HOW MANY spec-band bins left grading, these say
    WHICH ranges and WHY. Strings are copied, not composed here —
    ``carve_outs_by_band`` in ``crossover_v2_flow`` owns the copy, so this
    and the chart callouts render the same words; this only prefixes the
    band. Takes a compact cloud-phase BLOCK, not ``status``, so the caller
    picks which cloud.
    """
    lines: list[str] = []
    carve_outs = block.get("carve_outs")
    if not isinstance(carve_outs, list):
        return lines
    for band in carve_outs:
        if not isinstance(band, Mapping):
            continue
        expert = band.get("expert")
        if not isinstance(expert, str) or not expert:
            continue
        edges = band.get("band_hz")
        lo = _finite(edges[0]) if isinstance(edges, (list, tuple)) and edges else None
        hi = (
            _finite(edges[1])
            if isinstance(edges, (list, tuple)) and len(edges) == 2
            else None
        )
        where = f"{lo:.0f}–{hi:.0f} Hz " if lo is not None and hi is not None else ""
        lines.append(f"{where}{expert}")
    return lines


def _closing_envelope(status: Mapping[str, Any]) -> dict[str, Any]:
    """The measuring session's TAIL — measured, not yet proposed (D1, B2).
    True at two moments: ``awaiting_confirm`` (pre-apply cloud walked,
    group-close confirm open — household has something to do) and
    ``running`` (confirmed, combine+fit in flight — the one screen that
    sets ``busy``). Not the review screen. No SCREEN-LEVEL actions (all
    are destructive of in-progress work; Stop rides the capture block). The
    confirm belongs to the household here (#2881): mints Save/Record-again
    against ``/v2/complete``/``/v2/retake``, both ``show_during_capture``.
    NOT while a capture is held — a screen-level primary would suppress
    the walkthrough rendering the hold.
    """
    v2 = _v2(status)
    running = str(v2.get("cloud_close") or "") == CLOUD_CLOSE_RUNNING
    capture = _mapping(status.get("capture"))
    # Derived from durable ``cloud_close``, not the slot, so it also
    # renders after the walk ended un-confirmed. The two moves below POST
    # into signals the slot drops once out of an in-flight status.
    live = bool(
        str(capture.get("status") or "")
        and str(capture.get("status")) not in SESSION_ENDED_STATUSES
    )
    held = bool(capture.get("position_pending"))
    ready = live and not running and not held
    if running:
        verdict = (
            "JTS is working out your correction from the measurements — this "
            "takes a few seconds."
        )
    elif ready:
        verdict = (
            "All spots measured. Save this measurement, or record the last "
            "spot again."
        )
    elif live:
        # Held: the only way here with a hold open is a retake just asked for.
        verdict = "Re-recording one spot — follow the step below."
    else:
        verdict = (
            "All spots measured, but this measurement session has ended "
            "before it was saved. Measure again to keep a round."
        )
    return _envelope(
        screen="closing",
        active_step="measure",
        verdict=verdict,
        next_action={
            "id": "crossover_v2_complete",
            "label": "Save this measurement",
            "endpoint": "/sound/speaker/crossover/v2/complete",
            "body": {},
            "show_during_capture": True,
        } if ready else None,
        alternate_actions=[{
            "id": "crossover_v2_retake",
            "label": "Record the last spot again",
            "endpoint": "/sound/speaker/crossover/v2/retake",
            "body": {},
            "show_during_capture": True,
        }] if ready else [],
        busy=running,
        status=status,
        expert_details=_flatness_details_lines(status),
    )


def _cloud_verify_block(status: Mapping[str, Any]) -> Mapping[str, Any]:
    """The compact CLOUD-VERIFY entry of the ``cloud`` block, or empty.

    ``PHASE_CLOUD_VERIFY`` is spelled through the shared phase constant, not
    a literal, so this and the session cannot drift apart on the key name.
    """
    return _mapping(_mapping(_v2(status).get("cloud")).get(PHASE_CLOUD_VERIFY))


def _cloud_measure_block(status: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(_mapping(_v2(status).get("cloud")).get(PHASE_CLOUD_MEASURE))


def _flatness_unavailable_line(entry: Mapping[str, Any]) -> list[str]:
    """The honest gauge-absent rendering for a CLOUD-VERIFY block that
    CLOSED but carries no usable flatness. Two states: the pipeline DID
    run and carries no gauge (an older build; ``overall_within_target`` is
    ``None``), or it never became available (a combine/DSP-step failure).
    Neither quotes a number. A MISSING entry never reaches here (#1965) —
    :func:`_flatness_details_lines` routes that to
    :func:`_pre_apply_flatness_lines` first.
    """
    if entry.get("overall_within_target") is not None:
        return [
            "flatness not recorded for this measurement — it predates the "
            "spec gauge; re-measure to see it"
        ]
    return [
        "flatness not available for this measurement — the spatial "
        "measurement could not be analysed"
    ]


def _v2(status: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(status.get("crossover_v2"))


def _step_payload(active_step: str, done_steps: set[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for step_id in _STEP_IDS:
        rows.append({
            "id": step_id,
            "label": _STEP_LABELS[step_id],
            "status": (
                "done" if step_id in done_steps
                else "active" if step_id == active_step
                else "pending"
            ),
        })
    return rows


def _progress(active_step: str) -> dict[str, int]:
    try:
        position = _STEP_IDS.index(active_step) + 1
    except ValueError:
        position = len(_STEP_IDS)
    return {"position": position, "total": len(_STEP_IDS)}


def _done_before(active_step: str) -> set[str]:
    """Every step strictly before the active one is done (monotonic journey)."""
    try:
        frontier = _STEP_IDS.index(active_step)
    except ValueError:
        frontier = len(_STEP_IDS)
    return set(_STEP_IDS[:frontier])


def _applied_chip(status: Mapping[str, Any]) -> dict[str, str]:
    """Durable applied-crossover chip — reuse the legacy contract shape."""
    contract = _mapping(_mapping(status.get("setup")).get("applied_crossover"))
    if contract.get("valid") is not True:
        return {"state": "none", "label": "No speaker profile applied"}
    owner = str(contract.get("owner") or "")
    if owner == "automatic":
        return {"state": "automatic", "label": "Automatic crossover applied"}
    if owner == "manual":
        return {"state": "manual", "label": "Manual crossover applied"}
    return {"state": "applied", "label": "Speaker profile applied"}


def _setup_ready(status: Mapping[str, Any]) -> bool:
    setup = _mapping(status.get("setup"))
    return setup.get("active") is True and setup.get("status") == "ready"


def _envelope(
    *,
    screen: str,
    active_step: str,
    verdict: str,
    nudges: list[dict[str, str]] | None = None,
    next_action: dict[str, Any] | None = None,
    alternate_actions: list[dict[str, Any]] | None = None,
    status: Mapping[str, Any],
    expert_details: list[str] | None = None,
    advertise_capture: bool = True,
    busy: bool = False,
) -> dict[str, Any]:
    resting = screen in {"awaiting_plan", "finished"}
    return {
        "schema_version": CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION,
        "flow": "v2",
        "screen": screen,
        "active": True,
        "steps": _step_payload(active_step, _done_before(active_step)),
        "verdict_text": verdict,
        "nudges": nudges or [],
        # Optional collapsed expert-disclosure lines (#1605) — the frontend folds
        # them behind a <details>. Empty on every screen that has none.
        "expert_details": list(expert_details or []),
        # A terminal / restart screen must stop advertising the dead phone link
        # and its QR — the session it pointed at is gone.
        "capture": (_mapping(status.get("capture")) or None) if advertise_capture else None,
        "next_action": next_action,
        "alternate_actions": alternate_actions or [],
        # MACHINE-paced: speaker working, household waits. Declared for the
        # renderer; no renderer reads it yet. False except ``closing``'s
        # fit-in-flight moment.
        "busy": bool(busy),
        "progress": _progress(active_step),
        "applied": _applied_chip(status),
        "round": None,
        "candidate_review": None,
        # Compact per-group honesty verdict — the SAME projection
        # ``crossover_v2_status_block`` serves at ``/state``. ``None``
        # before any cloud group has closed.
        "cloud": None if resting else _v2(status).get("cloud"),
        # The before/after chart's decimated feed, kept off ``cloud`` so
        # the doctor (which reads only ``cloud``) never parses curve data.
        "cloud_chart": None if resting else _v2(status).get("cloud_chart"),
        "prediction": None,
        "findings": [],
    }


def _awaiting_plan_envelope(status: Mapping[str, Any]) -> dict[str, Any]:
    return _envelope(
        screen="awaiting_plan", active_step="microphone_check",
        verdict="No measurement is planned. Start one with jasper-round run "
                "(the LLM does this); this page joins it.",
        status=status, advertise_capture=False,
    )


def _failure_pilot_heard(status: Mapping[str, Any]) -> bool | None:
    """Whether the failed capture's pilot pair was heard — or ``None``, unknown.

    ``locate_failed``'s copy branches on this (#2085). ``None`` is a third
    state, not falsy — a failure that ran no capture simply does not say.
    """
    heard = _mapping(_v2(status).get("failure")).get("pilot_heard")
    return heard if isinstance(heard, bool) else None


def _reason_message(
    code: str, spec: ReasonSpec, status: Mapping[str, Any],
) -> str:
    """Use the registry's copy with recorded evidence (issues #1974, #2085)."""
    return reason_message(
        code, spec,
        pilot_heard=_failure_pilot_heard(status),
        reflection_measured=_verify_gate_reflection_measured(status),
    )


def _reset_action() -> dict[str, Any]:
    return {"id": "reset", "label": "Start over",
            "endpoint": "/sound/speaker/crossover/reset", "body": {}}


def _failure_envelope(code: str, status: Mapping[str, Any]) -> dict[str, Any]:
    spec = REASON_REGISTRY.get(code)
    action = dict(spec.next_action) if spec and spec.next_action else None
    return _envelope(
        screen="finished", active_step="verify",
        verdict=_reason_message(code, spec, status) if spec else "Measurement failed.",
        next_action=action or _reset_action(),
        alternate_actions=[_reset_action()] if action else [],
        status=status, advertise_capture=False,
    )


def build_crossover_envelope_v2(status: Mapping[str, Any]) -> dict[str, Any]:
    """The v2 session envelope for the served status.

    Stamped with :data:`CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION` rather than a
    number written down here.
    """
    if not bool(status.get("active")):
        return {
            "schema_version": CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION,
            "flow": "v2",
            "screen": "not_applicable",
            "active": False,
            "steps": [],
            "verdict_text": "This speaker has no active crossover.",
            "nudges": [],
            "capture": _mapping(status.get("capture")) or None,
            "next_action": None,
            "alternate_actions": [],
            "progress": {"position": 0, "total": len(_STEP_IDS)},
            "applied": _applied_chip(status),
            "round": None,
            "candidate_review": None,
            "cloud": None,
        }

    v2 = _v2(status)
    phase = str(v2.get("phase") or PHASE_CHECK)

    # Keys on needs_recovery, NOT unresolved_volume_safety alone: a
    # crash-hydrated active plan surfaces no unresolved payload but still
    # needs draining.
    if bool(v2.get("needs_recovery")):
        spec = REASON_REGISTRY[REASON_VOLUME_UNRESOLVED]
        return _envelope(
            screen="volume_recovery", active_step="microphone_check",
            verdict=spec.message,
            nudges=[{
                "code": "crossover_v2_volume_unresolved",
                "severity": "warn",
                "text": spec.message,
            }],
            next_action={
                "id": "recover_volume",
                "label": "Recover safe listening volume",
                "endpoint": "/sound/speaker/crossover/recover-volume",
                "body": {},
            },
            status=status,
        )

    # Speaker setup must be proven before any measurement plays.
    if not _setup_ready(status):
        return _envelope(
            screen="speaker_setup", active_step="speaker_setup",
            verdict=(
                "Finish the protected speaker setup first. This proves the output "
                "map and tweeter protection before the microphone check can play."
            ),
            next_action={"id": "speaker_setup", "label": "Finish speaker setup", "href": "/sound/speaker/"},
            status=status,
        )

    capture = _mapping(status.get("capture"))
    terminal = capture.get("status")
    run = _mapping(capture.get("run"))
    failure_code = str(run.get("fault") or _mapping(v2.get("failure")).get("code") or "")
    if terminal in SESSION_ENDED_STATUSES:
        if terminal == CAPTURE_FAILED:
            return _failure_envelope(failure_code, status)
        return _envelope(
            screen="finished", active_step="verify",
            verdict="Measurement complete." if terminal == CAPTURE_COMPLETE else "Measurement stopped.",
            next_action=_reset_action(), status=status, advertise_capture=False,
        )
    if not capture:
        return _failure_envelope(failure_code, status) if failure_code else _awaiting_plan_envelope(status)
    if capture.get("join") or (phase == PHASE_CHECK and capture):
        phase = PHASE_MEASURE
    active_step = _PHASE_STEP[phase]
    if phase == PHASE_CHECK:
        env = _awaiting_plan_envelope(status)
    elif phase == PHASE_MEASURE:
        env = _envelope(
            screen="measure", active_step=active_step,
            verdict=(
                "Keep the microphone still — JTS is measuring both drivers. Follow "
                "the measurement page; it continues automatically."
            ),
            next_action=None,
            status=status,
        )
    elif phase == PHASE_CLOUD_MEASURE:
        # Same wizard screen as MEASURE; verdict copy changes since the
        # point of this phase is moving the microphone, not holding still.
        env = _envelope(
            screen="measure", active_step=active_step,
            verdict=(
                "JTS is measuring from a few different spots — follow the "
                "step below. Moving the microphone between spots is what lets "
                "JTS tell the speaker apart from the room."
            ),
            next_action=None,
            status=status,
        )
    elif phase == PHASE_LATERAL:
        # R16's walk (§4.4). Bespoke copy: must state the return to the mark.
        env = _envelope(
            screen="measure", active_step=active_step,
            verdict=(
                "JTS is measuring from a few spots either side of the mark, "
                "and then back on it — follow the step below. Moving the "
                "microphone is what shows how the speaker's drivers hand over "
                "to each other away from the middle."
            ),
            next_action=None,
            status=status,
        )
    elif phase == PHASE_ENTRY_BASELINE:
        # #2291's "before" capture. "on the mark", not "BACK on the mark":
        # this follows MEASURE, where the microphone never left.
        env = _envelope(
            screen="measure", active_step=active_step,
            verdict=(
                "One last measurement, on the mark and held still — this "
                "is how your speaker sounds now, so JTS can tell you whether "
                "the tuning actually improved it."
            ),
            next_action=None,
            status=status,
        )
    elif phase == PHASE_VERIFY:
        verdict = (
            "The crossover is applied. Put the microphone back where it "
            "started and follow the measurement page to confirm the result"
        )
        # Express (M=1) has no post-apply cloud — this anchor is the WHOLE
        # post-apply check, not the first of several (§1.3). Full says nothing
        # extra here: its cloud walk follows.
        verdict += "."
        env = _envelope(
            screen="verify", active_step=active_step,
            verdict=verdict,
            next_action=None,
            status=status,
            expert_details=_flatness_details_lines(status),
        )
    elif phase == PHASE_CLOUD_VERIFY:
        env = _envelope(
            screen="verify", active_step=active_step,
            verdict=(
                "Checking the result from the same few spots — follow the "
                "prompts on the measurement page."
            ),
            next_action=None,
            status=status,
        )
    elif phase == PHASE_CLOSING:
        env = _closing_envelope(status)
    else:
        env = _awaiting_plan_envelope(status)

    log_event(
        logger, "correction.crossover_v2_envelope_serve",
        screen=env["screen"], phase=phase, failure="",
    )
    return env


def crossover_v2_phase(
    state: Mapping[str, Any] | None, *, review_declined: bool,
) -> str:
    """Project the durable journey phase, including old recorded declines."""
    accepted = set(
        state.get("accepted_phases") or () if isinstance(state, Mapping) else ()
    )
    applied = bool(state and state.get("applied"))
    recorded = state.get("session_phases") if isinstance(state, Mapping) else None
    known = (
        tuple(str(p) for p in recorded if str(p) in CAPTURE_PHASES)
        if isinstance(recorded, (list, tuple))
        else ()
    )
    phases = known or PRE_CLOUD_CAPTURE_PHASES
    for phase in phases:
        if phase not in accepted:
            if phase == PHASE_VERIFY and PHASE_MEASURE in accepted and not applied:
                return PHASE_APPLYING
            return phase
    if PHASE_VERIFY not in phases:
        if applied:
            candidate = state.get("candidate") if isinstance(state, Mapping) else None
            candidate_fingerprint = (
                candidate.get("fingerprint")
                if isinstance(candidate, Mapping) else None
            )
            if tuning_trial_matches_candidate(
                (state or {}).get("tuning_trial"), candidate_fingerprint,
            ):
                return PHASE_DONE
            return PHASE_VERIFY
        if str((state or {}).get("cloud_close") or ""):
            return PHASE_CLOSING
        if review_declined:
            return PHASE_CHECK
        return PHASE_REVIEW
    return PHASE_DONE


def _provenance_note(measured_this_session: bool | None) -> str:
    """PR-7's household-facing provenance caption — one owner of the copy, so
    the chart never has to (or may) phrase this itself.

    A re-armed session's ``persist_conductor_state`` can carry a group's
    ``cloud`` entry forward from an EARLIER session verbatim (see
    :func:`~jasper.active_speaker.crossover_v2.durable_state._cloud_summary`'s
    own comment and the B1 fix above it) — so
    ``/state.crossover_v2.cloud`` and the envelope can describe a measurement
    that did not happen in the session currently open on the page. Silently
    charting it as fresh would be exactly the kind of measured-narrow-
    stated-wide claim this program exists to avoid.

    ``""`` for both "definitely current" and "unknown" (a durable state
    written before this marker existed, or the whole entry unavailable) —
    mirrors :func:`~jasper.active_speaker.crossover_v2.spatial._geometry_guidance_copy`'s
    "empty string when nothing to say" rule rather than asserting freshness
    it cannot prove. Only the one state worth interrupting the household
    for — data that is KNOWN to be stale — gets a sentence.
    """
    if measured_this_session is False:
        return (
            "This chart is from a previous session's measurement — "
            "re-measure to see this session's own result."
        )
    return ""


def compact_cloud_status(
    cloud_state: Any,
    *,
    current_session_id: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(cloud_state, Mapping):
        return None
    out: dict[str, Any] = {}
    for phase, block in cloud_state.items():
        if not isinstance(block, Mapping):
            continue
        positions = block.get("positions")
        geometry = block.get("geometry")
        geometry = geometry if isinstance(geometry, Mapping) else {}
        pipeline = block.get("pipeline")
        pipeline = pipeline if isinstance(pipeline, Mapping) else {}
        produced_by = block.get("session_id")
        measured_this_session: bool | None = None
        if isinstance(produced_by, str) and produced_by and current_session_id:
            measured_this_session = produced_by == current_session_id
        entry: dict[str, Any] = {
            "geometry_locked": bool(geometry.get("locked")),
            "thin_evidence": bool(geometry.get("thin_evidence")),
            "geometry_guidance": _geometry_guidance_copy(geometry),
            "spec_bands": [],
            "overall_within_target": None,
            "excluded_interval_count": None,
            "flatness": None,
            "reference_db": None,
            "validity_floor_hz": None,
            "carve_outs": [],
            "provenance_note": _provenance_note(measured_this_session),
            "positions_accepted": (
                len(positions) if isinstance(positions, list) else None
            ),
            "positions_required": None,
        }
        if pipeline.get("available") is True:
            spec = pipeline.get("spec")
            spec = spec if isinstance(spec, Mapping) else {}
            bands = spec.get("bands")
            entry["spec_bands"] = [
                {
                    "f_lo_hz": b.get("f_lo_hz"),
                    "f_hi_hz": b.get("f_hi_hz"),
                    # The edges actually graded, beside the nominal ones. The
                    # top band's now follows the session's microphone-trust
                    # ceiling, so a row printing only the nominal pair states
                    # a span this evaluation did not grade.
                    "graded_lo_hz": b.get("graded_lo_hz"),
                    "graded_hi_hz": b.get("graded_hi_hz"),
                    "within_target": b.get("within_target"),
                    "max_deviation_db": b.get("max_deviation_db"),
                    # WHERE the worst bin sat. A dB with no frequency names
                    # no defect to fix.
                    "max_deviation_hz": b.get("max_deviation_hz"),
                    "tolerance_db": b.get("tolerance_db"),
                }
                for b in bands
                if isinstance(b, Mapping)
            ] if isinstance(bands, list) else []
            entry["overall_within_target"] = spec.get("overall_within_target")
            entry["reference_db"] = _finite(spec.get("reference_db"))
            merged = pipeline.get("merged_excluded_bands_hz")
            entry["excluded_interval_count"] = (
                len(merged) if isinstance(merged, list) else 0
            )
            flatness = pipeline.get("flatness")
            # Copied, never re-derived — see this function's docstring.
            entry["flatness"] = dict(flatness) if isinstance(flatness, Mapping) else None
            floor = pipeline.get("validity_floor_hz")
            entry["validity_floor_hz"] = (
                float(floor) if isinstance(floor, (int, float)) else None
            )
            carve_outs = pipeline.get("carve_outs")
            # Copied, never re-derived — same rule as ``flatness`` above. A
            # durable state written by a build BETWEEN PR-4 and PR-6b has an
            # available pipeline but no ``carve_outs`` key, and keeps the empty
            # default — indistinguishable here from a group that genuinely
            # carved nothing. ``excluded_interval_count`` is the tell for a
            # reader who needs to know: > 0 alongside an empty carve-out list
            # is the pre-PR-6b era, since a group that carved nothing has a
            # count of 0. No repair is attempted from this projection: it is
            # not an owner of the pipeline's data (see the docstring).
            entry["carve_outs"] = (
                [dict(band) for band in carve_outs if isinstance(band, Mapping)]
                if isinstance(carve_outs, list)
                else []
            )
        out[str(phase)] = entry
    return out or None


CHART_CURVE_MAX_JSON_POINTS = 256


def decimate_curve_for_chart(freqs: Any, mags: Any) -> dict[str, Any] | None:
    """Stride a stored curve down to at most :data:`CHART_CURVE_MAX_JSON_POINTS`.

    THE chart feed's decimation — extracted from :func:`chart_cloud_status`'s
    body (two-stage commission D4) when the predicted curve became a second
    curve on the same block. D4 asks for the prediction to ride "the existing
    ``CHART_CURVE_MAX_JSON_POINTS`` path so the chart feed keeps one decimation
    owner"; a second inline copy of this stride would be a second owner, and
    two curves drawn in one frame at silently different densities is exactly
    the drift that costs. ``None`` for anything that is not a usable pair, so a
    caller never fabricates an empty curve out of malformed state.

    **Ceiling-division stride, not floor (gate finding on #1858, SF-1).** The
    original shape here was ``step = n // CAP`` — a *soft* ceiling, documented
    (and pinned, before this fix) as capable of overshooting by up to one
    stride: 1031 raw points strode by 4 and yielded 258, not 256. That was
    tolerable while every caller's persisted length always landed at or above
    ``CAP * 2`` (both ``_decimate_sum``'s old raw stride and
    ``_decimate_curve_for_json``'s stride always overshoot to slightly above
    their own 512-point cap). #1858's block-average fix to ``_decimate_sum``
    changed that: block-averaging *undershoots* its cap instead of
    overshooting it (a 32769-bin capture landed at 504, not 512-513), which
    put the predicted curve's persisted length just BELOW ``CAP * 2`` — where
    floor division gives ``step = 1``, i.e. no reduction at all (504 points
    rendered, not ~252), breaking the soft-ceiling promise outright and
    rendering the prediction at roughly double the cloud curves' density in
    the same chart frame. Ceiling division (``-(-n // CAP)``, this module's
    existing integer-ceiling idiom — see
    :func:`~jasper.audio_measurement.spatial_combine._decimate_to_analysis_grid`)
    makes ``len(rendered) <= CAP`` a TRUE hard bound for any input length,
    closing the whole class rather than this one instance: it guarantees
    ``step >= n / CAP`` by construction, so ``ceil(n / step) <= CAP`` always.
    Both curve families now render through the identical formula, so neither
    can silently outrun the other's density regardless of which side of any
    boundary their own persisted length lands on.

    **One deliberate behaviour delta from the inlined version this replaced.**
    A zero-length pair used to yield ``{"freqs_hz": [], "magnitude_db": []}``;
    it now yields ``None``. Reachable only from malformed durable state — a
    pipeline marked ``available: True`` whose stored curve is empty — and the
    new answer is the honest direction: an empty curve renders as "we looked
    and there is nothing there", which is the fabricated-clean-reading shape
    this module forbids, whereas ``None`` says "no curve", which is what an
    empty stored curve actually means.
    """
    if not isinstance(freqs, list) or not isinstance(mags, list):
        return None
    n = min(len(freqs), len(mags))
    if n == 0:
        return None
    step = max(1, -(-n // CHART_CURVE_MAX_JSON_POINTS))
    return {
        "freqs_hz": [_finite(f) for f in freqs[:n:step]],
        "magnitude_db": [_finite(m) for m in mags[:n:step]],
    }


def chart_cloud_status(cloud_state: Any) -> dict[str, Any] | None:
    """Bounded live curves using the shared display projection; absent curves stay None."""
    if not isinstance(cloud_state, Mapping):
        return None
    out: dict[str, Any] = {}
    for phase, block in cloud_state.items():
        if not isinstance(block, Mapping):
            continue
        pipeline = block.get("pipeline")
        pipeline = pipeline if isinstance(pipeline, Mapping) else {}
        curve = None
        if pipeline.get("available") is True:
            raw_curve = pipeline.get("curve")
            if isinstance(raw_curve, Mapping):
                curve = decimate_curve_for_chart(
                    raw_curve.get("freqs_hz"), raw_curve.get("magnitude_db"),
                )
                spec = pipeline.get("spec")
                if curve is not None:
                    curve = prepare_frequency_curve({**curve, "band_hz": raw_curve.get("band_hz")}, {
                        **pipeline,
                        "reference_db": spec.get("reference_db") if isinstance(spec, Mapping) else None,
                        "excluded_bands_hz": pipeline.get("merged_excluded_bands_hz"),
                    })
        out[str(phase)] = {"curve": curve}
    return out or None


def prediction_status(state: Any) -> dict[str, Any] | None:
    """The PREDICTED post-apply response and its stored spec verdict, or
    ``None`` (two-stage commission D4).

    Rides the adapter's returned dict beside ``cloud`` /
    ``cloud_chart``. Both halves were already computed — the curve by
    ``_decimate_sum`` at persist time, the verdict by the conductor's
    accountability seam against the FULL-RESOLUTION tuple — and neither reached
    any surface. This projects; it never grades.

    **Nothing renders it yet.** It is the wire half of the two-stage flow's
    review screen (the "what we predict" panel and the chart's third
    curve), landed on its own rung so that screen is built against data already
    proven on the wire rather than against a shape invented alongside it.

    **``curve`` and ``spec`` are independently absent, and all four
    combinations are reachable.** Enumerated because a consumer — the review
    screen above all — has to render each one differently:

    1. *Both present* — the ordinary closed session. Draw the curve, state the
       verdict.
    2. *Curve, no report* — a state written before D4, or a prediction the
       evaluator refused (:func:`~jasper.active_speaker.crossover_v2_flow
       .spec_report_for_predicted_sum` returned ``None``). Draw the curve, say
       the verdict is unknown; **do not** infer one from the picture.
    3. *Neither* — no session has closed a candidate. This function returns
       ``None`` outright rather than an empty shell.
    4. *Report, no curve* — **the least obvious of the four.**
       ``_assert_accountable`` stashes the verdict BEFORE the improvement gate
       runs and ``_measure_predicted_sum`` only after it returns, so a refusal
       between the two persists a report with ``predicted_sum`` still ``None``
       — honest, not a leak: the spec verdict did evaluate that prediction. The
       refusal that produced this shape is retired (``accountability``'s item
       2); a pre-retirement state still carries it, and a consumer shows the
       verdict with no curve to draw.

    So ``overall_within_target`` is ``None`` — not ``False`` — whenever no report was
    stored, under the same never-fabricate-a-clean-reading rule
    :func:`compact_cloud_status` states at length. ``None`` here means
    "unknown", and a consumer must not read it as permission. ``False`` is the
    opposite: a real graded verdict that the prediction misses the spec, which
    is exactly what state 4 carries.

    ``spec_bands`` / ``reference_db`` mirror the compact cloud block's own
    vocabulary key-for-key on purpose: the review screen draws the measured
    curve and this one in ONE deviation frame with one tolerance corridor, and
    a second spelling of the same five per-band numbers is how the two frames
    would drift apart.
    """
    priors = (state or {}).get("verify_priors")
    if not isinstance(priors, Mapping):
        return None
    raw_curve = priors.get("predicted_sum")
    spec = priors.get("predicted_spec")
    spec = spec if isinstance(spec, Mapping) else {}
    curve = None
    if isinstance(raw_curve, Mapping):
        curve = decimate_curve_for_chart(raw_curve.get("freqs_hz"), raw_curve.get("magnitude_db"))
        if curve is not None:
            curve = prepare_frequency_curve(
                {**curve, "band_hz": raw_curve.get("band_hz")},
                {**spec, "excluded_bands_hz": spec.get("excluded_intervals")},
            )
    if curve is None and not spec:
        return None
    bands = spec.get("bands")
    return {
        "curve": curve,
        "spec_bands": [
            {
                "f_lo_hz": b.get("f_lo_hz"),
                "f_hi_hz": b.get("f_hi_hz"),
                "within_target": b.get("within_target"),
                "max_deviation_db": b.get("max_deviation_db"),
                "tolerance_db": b.get("tolerance_db"),
            }
            for b in bands
            if isinstance(b, Mapping)
        ] if isinstance(bands, list) else [],
        "overall_within_target": (
            spec.get("overall_within_target")
            if isinstance(spec.get("overall_within_target"), bool)
            else None
        ),
        "reference_db": _finite(spec.get("reference_db")),
        "comparison": (
            dict(spec["comparison"])
            if isinstance(spec.get("comparison"), Mapping) else None
        ),
    }


def household_findings_status(state: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The banked findings a household may read, from the durable projection.

    Reads what :func:`~jasper.web.correction_crossover_v2._bank_household_findings`
    wrote — never the bundle, and
    never ``os``-anything: this runs on every wizard poll, and the read that
    costs (reopen + re-hash the artifact and its citation) already happened
    once, at publish.

    **Validated, not trusted.** The state file is JSON written by some build,
    possibly an older or newer one, so every row is checked rather than passed
    through: a row without usable copy is DROPPED (an empty or non-string
    sentence is not a finding a household can read), and an unusable ``at``
    becomes ``None`` — which the envelope renders as "we cannot say when",
    exactly as an undated failure record does. Fabricating neither a sentence
    nor a date is the whole contract here, and it is pinned at THIS layer
    (``tests/test_correction_crossover_v2_endpoints.py``'s projection-contract
    tests) rather than only through the envelope: a weakened copy check here —
    ``str(row.get("household_copy") or "")`` — renders a fabricated ``"42"`` on
    the done screen end to end, and every screen-level assertion stays green
    while it does.

    Never raises. ``at`` goes through
    :func:`~jasper.json_fields.finite_float`, which is where the
    unbounded-JSON-integer ``OverflowError`` is absorbed; this runs on the
    wizard's 1.5 s poll path, so an escaping conversion would be a 500 on a
    plain page load — the same failure :func:`_record_when_phrase` above
    catches for the same reason.
    """
    evidence = (state or {}).get("evidence")
    rows = (
        evidence.get(FINDING_HOUSEHOLD_REFS_KEY)
        if isinstance(evidence, Mapping)
        else None
    )
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        copy = row.get("household_copy")
        if not isinstance(copy, str) or not copy.strip():
            continue
        out.append({"household_copy": copy, "at": _finite(row.get("at"))})
    return out
