# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Server-computed *screen envelope* for the room-correction wizard.

The dumb-frontend / smart-backend contract (revision plan §3.2): the
browser is a pure renderer, and the Pi hands it one JSON object per
step describing everything to draw — which logical screen we're on,
the display curves (already server-smoothed), the two-tone before/after
fill, a one-number headline, plain-language verdict text, homeowner
nudges (a sentence + a checkmark, never a block), the next action, and
step progress.

This module is a pure presentation boundary. It reads a live
:class:`~jasper.correction.session.MeasurementSession` and derives the
envelope; it does not mutate the session and does not touch the existing
``/status`` payload (:func:`jasper.correction.status.session_snapshot`).
The Room page renders its exact ordered ``sections`` list while ``/status``
continues to drive capture, upload, and autolevel mechanics.

What is *not* duplicated here:
  - the before/after math (`fill_segments`, the measured delta) is the
    P3a machinery on the session as ``verify_before_after``; we relay it,
    we do not recompute it.
  - the confidence / capture-quality assessment is
    ``session.confidence_report`` (built by
    :func:`jasper.correction.confidence.build_confidence_report`); nudges
    are a homeowner-language *rendering* of its findings, not a second
    analysis.

Curves *are* smoothed here for display: the empirical curves
(``measured`` / ``predicted`` / ``verify``) are 1/N-octave smoothed via
the shared kernel so the browser never draws a raw jagged line (§3.2:
"Never show a raw jagged curve — server-smoothed"). Smoothing preserves
the grid (same length, same frequencies), so the ``fill_segments``
``i_lo``/``i_hi`` indices — computed on the raw grid — still address the
right display points (the coupling note in
:func:`jasper.audio_measurement.analysis.before_after_fill_segments`).
"""
from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np

from jasper.audio_measurement import analysis

from ..log_event import log_event

logger = logging.getLogger(__name__)

# Bumped independently of the bundle schema; a pinning test guards it.
# v2 (P4) adds the top-level `verdict` block — the deterministic
# accept/surface/revert_pending_confirm/revert decision, its reasons, and the
# per-band table — and folds the verdict into the RESULT-screen verdict_text.
# v3 (P5) adds the crossover-region distinction: the REVIEW verdict_text and a
# `crossover_region_dip_not_boosted` nudge explain that a dip AT the
# bass-management corner is the crossover, not a room mode (both derived from
# strategy.design_correction's `crossover_region` design-report annotation).
# v4 (P6) adds the `tuning_llm` block: whether the "Ask the tuning assistant"
# affordance shows on the review/apply/result screens (available when an
# OpenAI key is configured; hidden-with-nudge otherwise). Availability only,
# no paid call — the endpoints are per-tap and confirm-gated.
# v5 wires the relay-owned level-before-sweep actions.
# v6 makes the ordered `sections` list the sole whole-page visibility
# authority. The browser maps this fixed vocabulary to DOM nodes; it does not
# carry a second screen-to-section policy. v7 adds the closed `blocker` and
# `failure` presentation blocks so raw readiness/session diagnostics never
# become homeowner copy. v8 adds the server-owned `run_defaults` block: the
# disclosed run choices, capture transport, and whether Change is still legal.
ENVELOPE_SCHEMA_VERSION = 8

# 1/N-octave smoothing applied to the empirical display curves. 6 =
# 1/6-octave — visibly smoothed (no raw jaggedness) while preserving
# modal-range structure the homeowner should see. The target curve is
# smooth by construction (a designed target) and is passed through.
DISPLAY_SMOOTHING_FRACTION = 6

# The eight logical wizard screens (§3.2 flow). "level" is the §3.1
# level-match ramp; it is surfaced from the autolevel sub-state, not a
# distinct session state.
SCREEN_IDLE = "idle"
SCREEN_MIC = "mic"
SCREEN_LEVEL = "level"
SCREEN_SWEEP = "sweep"
SCREEN_REVIEW = "review"
SCREEN_APPLY = "apply"
SCREEN_VERIFY = "verify"
SCREEN_RESULT = "result"

# Room-owned whole-page section vocabulary. These names are a wire contract:
# the browser fails closed on any unsupported name instead of guessing what a
# future server meant. Order matters because it is also the page order.
SECTION_CURRENT_CORRECTION = "current-correction"
SECTION_RUN_DEFAULTS = "run-defaults"
SECTION_READINESS_BLOCKER = "readiness-blocker"
SECTION_CAPTURE_HANDOFF = "capture-handoff"
SECTION_PLACEMENT = "placement"
SECTION_CAPTURE_SETUP = "capture-setup"
SECTION_LOCAL_CERTIFICATE_WARNING = "local-certificate-warning"
SECTION_LEVEL_CHECK = "level-check"
SECTION_POSITION_CAPTURE = "position-capture"
SECTION_MEASUREMENT_REVIEW = "measurement-review"
SECTION_APPLY_STATUS = "apply-status"
SECTION_VERIFICATION = "verification"
SECTION_RESULT_PROOF = "result-proof"
SECTION_TUNING = "tuning"
SECTION_REPORTS = "reports"

SECTION_VOCABULARY = frozenset({
    SECTION_CURRENT_CORRECTION,
    SECTION_RUN_DEFAULTS,
    SECTION_READINESS_BLOCKER,
    SECTION_CAPTURE_HANDOFF,
    SECTION_PLACEMENT,
    SECTION_CAPTURE_SETUP,
    SECTION_LOCAL_CERTIFICATE_WARNING,
    SECTION_LEVEL_CHECK,
    SECTION_POSITION_CAPTURE,
    SECTION_MEASUREMENT_REVIEW,
    SECTION_APPLY_STATUS,
    SECTION_VERIFICATION,
    SECTION_RESULT_PROOF,
    SECTION_TUNING,
    SECTION_REPORTS,
})

_SCREEN_SECTIONS: dict[str, tuple[str, ...]] = {
    SCREEN_IDLE: (
        SECTION_CURRENT_CORRECTION,
        SECTION_RUN_DEFAULTS,
    ),
    SCREEN_MIC: (
        SECTION_RUN_DEFAULTS,
        SECTION_CAPTURE_HANDOFF,
        SECTION_PLACEMENT,
    ),
    SCREEN_LEVEL: (
        SECTION_CAPTURE_HANDOFF,
        SECTION_PLACEMENT,
        SECTION_LEVEL_CHECK,
    ),
    SCREEN_SWEEP: (
        SECTION_CAPTURE_HANDOFF,
        SECTION_PLACEMENT,
        SECTION_POSITION_CAPTURE,
    ),
    SCREEN_REVIEW: (SECTION_MEASUREMENT_REVIEW,),
    SCREEN_APPLY: (SECTION_APPLY_STATUS,),
    SCREEN_VERIFY: (
        SECTION_CAPTURE_HANDOFF,
        SECTION_PLACEMENT,
        SECTION_VERIFICATION,
    ),
    SCREEN_RESULT: (
        SECTION_CURRENT_CORRECTION,
        SECTION_RESULT_PROOF,
    ),
}

REPORT_SECTION_SCREENS = frozenset({SCREEN_IDLE, SCREEN_RESULT})


class _ReadinessUnset:
    pass


_READINESS_UNSET = _ReadinessUnset()

# Session state value -> logical screen. Keyed by the string value of
# SessionState (session.py) so this map does not import the enum and so
# a pinning test can assert every state is covered. "idle" doubles as
# the pre-flow home where the mic/calibration/level nudges live; the
# level-match ramp temporarily re-labels it "level" (see _screen_for).
_STATE_SCREEN: dict[str, str] = {
    "idle": SCREEN_IDLE,
    "needs_noise_capture": SCREEN_MIC,
    "preparing": SCREEN_SWEEP,
    "sweeping": SCREEN_SWEEP,
    "awaiting_capture": SCREEN_SWEEP,
    "needs_repeat_capture": SCREEN_SWEEP,
    "awaiting_repeat_capture": SCREEN_SWEEP,
    "needs_next_position": SCREEN_SWEEP,
    "analyzing": SCREEN_REVIEW,
    "ready": SCREEN_REVIEW,
    "applied": SCREEN_APPLY,
    "verifying": SCREEN_VERIFY,
    "awaiting_verify_capture": SCREEN_VERIFY,
    "verified": SCREEN_RESULT,
    "failed": SCREEN_RESULT,
}

# Ordered logical screens for the progress indicator. "mic"/"level" are
# pre-sweep sub-steps of the same "get set up" phase, so the ordered
# spine the homeowner walks is entry → measure → review → apply →
# verify → done. Kept deliberately coarse (§4: dumb frontend renders it).
_PROGRESS_SPINE: tuple[str, ...] = (
    SCREEN_IDLE,
    SCREEN_SWEEP,
    SCREEN_REVIEW,
    SCREEN_APPLY,
    SCREEN_VERIFY,
    SCREEN_RESULT,
)
# Screens that collapse onto a spine position for progress purposes.
_PROGRESS_ALIAS: dict[str, str] = {
    SCREEN_MIC: SCREEN_IDLE,
    SCREEN_LEVEL: SCREEN_IDLE,
}

# next_action per screen: the single button the dumb frontend offers.
# endpoint is the existing POST route the current page already uses, so
# the migrated page keeps the same server contract. label is homeowner
# copy. None means "no forward action from here" (terminal / waiting on
# a browser upload the page drives itself).
_NEXT_ACTION: dict[str, dict[str, str] | None] = {
    SCREEN_IDLE: {"label": "Start measuring", "endpoint": "/start"},
    SCREEN_MIC: None,          # browser drives the noise/mic upload
    SCREEN_LEVEL: None,        # ramp runs to a lock on its own
    SCREEN_SWEEP: None,        # browser drives the sweep capture upload
    SCREEN_REVIEW: {"label": "Apply correction", "endpoint": "/apply"},
    SCREEN_APPLY: {"label": "Verify the result", "endpoint": "/verify"},
    SCREEN_VERIFY: None,       # browser drives the verify capture upload
    SCREEN_RESULT: {"label": "Measure again", "endpoint": "/start"},
}


def screen_for_state(state_value: str) -> str:
    """Map a bare :class:`SessionState` value to a logical screen.

    Public so the pinning test can assert total coverage without
    reaching into the session. Unknown values fail closed: treating a new
    backend state as idle would incorrectly offer Start and discard the
    in-flight session's real meaning.
    """
    try:
        return _STATE_SCREEN[state_value]
    except KeyError as exc:
        raise ValueError(f"unsupported room-correction state: {state_value}") from exc


def _screen_for(session: Any) -> str:
    """Resolve the live screen, folding in the level-match sub-state.

    The room session has no dedicated "level" state. For local capture the
    first ``needs_noise_capture`` stop means mic setup until the realized
    input is bound, then level matching until the first noise upload. Later
    positions have already crossed both setup gates and are sweep screens.
    Relay capture folds its separate level-match snapshot here as before.
    """
    screen = screen_for_state(session.state.value)
    if (
        getattr(session, "capture_transport", "local") != "relay"
        and session.state.value == "needs_noise_capture"
        and bool(getattr(session, "local_capture_setup_bound", False))
    ):
        return (
            SCREEN_LEVEL
            if int(getattr(session, "current_position", 0) or 0) == 0
            else SCREEN_SWEEP
        )
    if (
        getattr(session, "capture_transport", "local") == "relay"
        and session.state.value == "needs_noise_capture"
    ):
        return SCREEN_MIC if _relay_level_ready(session) else SCREEN_LEVEL
    if (
        getattr(session, "capture_transport", "local") == "relay"
        and (
            session.state.value == "applied"
            or (
                session.state.value == "verified"
                and _relay_confirmation_pending(session)
            )
        )
        and not _relay_level_ready(session)
    ):
        return SCREEN_LEVEL
    if screen == SCREEN_IDLE:
        autolevel = _autolevel_snapshot(session)
        if autolevel.get("status") == "ramping":
            return SCREEN_LEVEL
    return screen


def screen_for_session(session: Any) -> str:
    """Return the logical screen for a live session without mutating it."""
    return _screen_for(session)


def _sections_for(
    screen: str,
    *,
    capture_transport: str,
    reports_available: bool,
    tuning_offered: bool,
    readiness_blocked: bool,
) -> list[str]:
    """Build the exact ordered whole-page section list for one snapshot."""
    sections = (
        [SECTION_CURRENT_CORRECTION, SECTION_READINESS_BLOCKER]
        if screen == SCREEN_IDLE and readiness_blocked
        else list(_SCREEN_SECTIONS[screen])
    )
    if screen == SCREEN_MIC and capture_transport == "local":
        sections.extend((
            SECTION_LOCAL_CERTIFICATE_WARNING,
            SECTION_CAPTURE_SETUP,
        ))
    if tuning_offered:
        sections.append(SECTION_TUNING)
    if reports_available and screen in REPORT_SECTION_SCREENS:
        sections.append(SECTION_REPORTS)
    return sections


def _level_match_snapshot(session: Any) -> dict[str, Any]:
    try:
        snap = session.level_match_snapshot()
    except (AttributeError, RuntimeError, TypeError):
        return {}
    return snap if isinstance(snap, dict) else {}


def _relay_level_ready(session: Any) -> bool:
    level = _level_match_snapshot(session)
    last = level.get("last") if isinstance(level, dict) else None
    ramp = last.get("ramp") if isinstance(last, dict) else None
    return bool(
        isinstance(ramp, dict)
        and ramp.get("state") == "locked"
    )


def _relay_confirmation_pending(session: Any) -> bool:
    acceptance = getattr(session, "acceptance", None)
    return bool(
        isinstance(acceptance, dict)
        and str(acceptance.get("verdict") or "") == "revert_pending_confirm"
    )


def _autolevel_snapshot(session: Any) -> dict[str, Any]:
    al = getattr(session, "autolevel", None)
    if al is None:
        return {}
    try:
        snap = al.snapshot()
    except (AttributeError, RuntimeError, TypeError):
        # Never let a bad sub-snapshot break the envelope: a duck-typed
        # session may lack snapshot(), and a mid-teardown controller can
        # raise — the envelope just omits the level fold.
        return {}
    return snap if isinstance(snap, dict) else {}


def _progress(screen: str) -> dict[str, int]:
    spine_screen = _PROGRESS_ALIAS.get(screen, screen)
    try:
        position = _PROGRESS_SPINE.index(spine_screen) + 1
    except ValueError:
        position = 1
    return {"position": position, "total": len(_PROGRESS_SPINE)}


def _smoothed_curve(
    curve: Any, *, smooth: bool
) -> dict[str, list[float]] | None:
    """Render one ``CurveJSON`` as a display curve.

    ``smooth`` applies 1/N-octave smoothing (empirical curves); the
    target passes through unsmoothed (already smooth by construction).
    Returns ``None`` when the curve is absent or malformed so the
    browser simply omits that trace.
    """
    if curve is None:
        return None
    freqs = getattr(curve, "freqs_hz", None)
    mags = getattr(curve, "magnitude_db", None)
    if not freqs or not mags or len(freqs) != len(mags):
        return None
    if not smooth:
        return {
            "freqs_hz": [float(f) for f in freqs],
            "magnitude_db": [float(m) for m in mags],
        }
    freqs_arr = np.asarray(freqs, dtype=np.float64)
    mags_arr = np.asarray(mags, dtype=np.float64)
    smoothed = analysis.smooth_fractional_octave(
        freqs_arr, mags_arr, fraction=DISPLAY_SMOOTHING_FRACTION
    )
    return {
        "freqs_hz": freqs_arr.tolist(),
        "magnitude_db": smoothed.tolist(),
    }


def _curves(session: Any) -> dict[str, dict[str, list[float]]]:
    """Server-smoothed display curves; only present traces are included."""
    out: dict[str, dict[str, list[float]]] = {}
    measured = _smoothed_curve(
        getattr(session, "measured_curve", None), smooth=True
    )
    if measured is not None:
        out["measured"] = measured
    target = _smoothed_curve(
        getattr(session, "target_curve", None), smooth=False
    )
    if target is not None:
        out["target"] = target
    predicted = _smoothed_curve(
        getattr(session, "predicted_curve", None), smooth=True
    )
    if predicted is not None:
        out["predicted"] = predicted
    verify = _smoothed_curve(
        getattr(session, "verify_curve", None), smooth=True
    )
    if verify is not None:
        out["verify"] = verify
    return out


def _band_word(f_low: float, f_high: float) -> str:
    """Plain-language name for the correction band the numbers cover."""
    if f_high <= 250.0:
        return "the bass"
    if f_high <= 500.0:
        return "the bass and lower mids"
    return "the low end"


def _headline(session: Any) -> dict[str, Any] | None:
    """One-number before/after headline from P3a's measured delta.

    Reads ``session.verify_before_after`` (the honest MEASURED delta over
    one consistent band) — never the predicted design number. Absent
    until a verify capture has landed.
    """
    vba = getattr(session, "verify_before_after", None)
    if not isinstance(vba, dict):
        return None
    before = vba.get("before")
    after = vba.get("after")
    delta = vba.get("delta")
    band = vba.get("band_hz")
    if not (
        isinstance(before, dict)
        and isinstance(after, dict)
        and isinstance(delta, dict)
        and isinstance(band, (list, tuple))
        and len(band) == 2
    ):
        return None
    before_max = before.get("max_db")
    after_max = after.get("max_db")
    if not isinstance(before_max, (int, float)) or not isinstance(
        after_max, (int, float)
    ):
        return None
    band_word = _band_word(float(band[0]), float(band[1]))
    text = (
        f"±{float(before_max):.0f} dB → "
        f"±{float(after_max):.0f} dB in {band_word}."
    )
    return {
        "before_max_db": float(before_max),
        "after_max_db": float(after_max),
        "rms_delta_db": _opt_float(delta.get("rms_db")),
        "max_delta_db": _opt_float(delta.get("max_db")),
        "band_hz": [float(band[0]), float(band[1])],
        "text": text,
    }


def _opt_float(value: Any) -> float | None:
    """Coerce a numeric value to float, or None if it isn't numeric."""
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _fill_segments(session: Any) -> list[dict[str, Any]]:
    """Relay P3a's server-computed two-tone fill (never recompute)."""
    vba = getattr(session, "verify_before_after", None)
    if not isinstance(vba, dict):
        return []
    segments = vba.get("fill_segments")
    return list(segments) if isinstance(segments, list) else []


# --------------------------------------------------------------------------
# Nudges — homeowner-language, severity info|warn, NEVER a block.
# --------------------------------------------------------------------------
#
# Each confidence-report finding code we surface maps to one plain-English
# sentence. Copy tone (§0.2, §3.2): measurement-quality nudges inform, they do
# not gate. Unknown codes and `fail` findings are omitted here: the former are
# diagnostics until Room assigns safe copy, while the latter belong to the
# blocking/failure path and must never be softened into an optional warning.
_NUDGE_COPY: dict[str, dict[str, str]] = {
    "uncalibrated_mic": {
        "severity": "info",
        "text": (
            "Results will be approximate without a calibrated mic — "
            "you can continue."
        ),
    },
    "single_position": {
        "severity": "info",
        "text": (
            "You measured one spot. Measuring a few more around your "
            "listening area makes the fix more accurate — but this is fine "
            "to continue."
        ),
    },
    "two_positions": {
        "severity": "info",
        "text": (
            "Two spots measured. Three or more sharpens the result, though "
            "you can continue now."
        ),
    },
    "incomplete_position_set": {
        "severity": "info",
        "text": (
            "Not every planned spot was measured — the result still works, "
            "just with a little less certainty."
        ),
    },
    "missing_input_device": {
        "severity": "info",
        "text": (
            "Your browser didn't share mic details, so we can't double-check "
            "the input — this doesn't block anything."
        ),
    },
    "moderate_position_variance": {
        "severity": "info",
        "text": (
            "Your measured spots differ a bit from each other, which is "
            "normal for a room. The measurement can still be used."
        ),
    },
    "high_position_variance": {
        "severity": "warn",
        "text": (
            "Your measured spots differ a lot, which can happen in a lively "
            "or uneven room. Measuring a tighter listening area may help, "
            "but you can continue."
        ),
    },
    "capture_snr_low": {
        "severity": "warn",
        "text": (
            "The room was a little noisy during the sweep. Quieting the room "
            "and keeping the microphone clear improves accuracy — you can "
            "still continue."
        ),
    },
    "runtime_integrity_warnings": {
        "severity": "warn",
        "text": (
            "The speaker noticed a timing or playback warning. Re-measuring "
            "may improve confidence, but you can continue."
        ),
    },
    "capture_quality_warnings": {
        "severity": "warn",
        "text": (
            "One capture was less clear than preferred. A quieter re-measure "
            "may improve confidence, but you can continue."
        ),
    },
    "repeatability_low": {
        "severity": "warn",
        "text": (
            "The main-seat repeat differed more than expected. Keeping the "
            "microphone still and re-measuring may help, but you can continue."
        ),
    },
    "repeatability_medium": {
        "severity": "info",
        "text": (
            "The main-seat repeat was usable, though not an exact match. You "
            "can continue."
        ),
    },
    "repeatability_unavailable": {
        "severity": "info",
        "text": (
            "The main-seat trust check was unavailable, so confidence is a "
            "little lower. You can continue."
        ),
    },
    "browser_processing_reported": {
        "severity": "warn",
        "text": (
            "The browser reported an audio-path limitation. A local "
            "measurement microphone can improve accuracy, but you can "
            "continue."
        ),
    },
}


def _nudges(session: Any) -> list[dict[str, str]]:
    """Homeowner-language nudges.

    Never a block: the strongest nudge is ``warn``. Confidence-report findings
    come first (in the report's own most-impactful-first order); the
    design-report crossover-region nudge is appended after. A session with a
    design report but no confidence report still surfaces the crossover nudge.
    """
    nudges: list[dict[str, str]] = []
    seen: set[str] = set()
    level_nudge = _bounded_low_level_nudge(session)
    if level_nudge is not None:
        nudges.append(level_nudge)
        seen.add(level_nudge["code"])
    report = getattr(session, "confidence_report", None)
    findings = report.get("findings") if isinstance(report, dict) else None
    if isinstance(findings, list):
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            code = str(finding.get("code") or "")
            if not code or code in seen:
                continue
            seen.add(code)
            if finding.get("severity") == "fail":
                continue
            canned = _NUDGE_COPY.get(code)
            if canned is None:
                continue
            nudges.append(
                {
                    "code": code,
                    "severity": canned["severity"],
                    "text": canned["text"],
                }
            )
    # Append the crossover-region nudge from the DESIGN report (not the
    # confidence report) so the homeowner learns a crossover-region dip was left
    # un-boosted on purpose. Additive; only present when a boost was excluded
    # there. Never a block (info severity).
    design_nudge = _crossover_region_nudge(session)
    if design_nudge is not None and design_nudge["code"] not in seen:
        nudges.append(design_nudge)
    return nudges


def _bounded_low_level_nudge(session: Any) -> dict[str, str] | None:
    """Surface a safe-but-quiet room level lock without blocking the sweep."""
    level = _level_match_snapshot(session)
    last = level.get("last") if isinstance(level, dict) else None
    ramp = last.get("ramp") if isinstance(last, dict) else None
    if not isinstance(ramp, dict) or ramp.get("lock_kind") != "bounded_low_level":
        return None
    shortfall = ramp.get("window_shortfall_db")
    if (
        not isinstance(shortfall, (int, float))
        or isinstance(shortfall, bool)
        or not math.isfinite(float(shortfall))
        or float(shortfall) <= 0.0
    ):
        return None
    return {
        "code": "bounded_low_measurement_level",
        "severity": "warn",
        "text": (
            "The microphone level is stable and safe but lower than preferred "
            f"({float(shortfall):.1f} dB below the preferred window). JTS will "
            "verify each sweep before using it."
        ),
    }


def _crossover_region_nudge(session: Any) -> dict[str, str] | None:
    """The homeowner nudge for a crossover-region dip left un-boosted, or None.

    Reads strategy.design_correction's ``crossover_region`` annotation off the
    session's design report (fail-soft). Mirrors the design warning
    ``crossover_region_dip_not_boosted`` — surfacing the same distinction the
    verdict_text makes, in nudge form."""
    report = getattr(session, "design_report", None)
    if not isinstance(report, dict):
        return None
    region = report.get("crossover_region")
    if not isinstance(region, dict):
        return None
    excluded = region.get("excluded_boosts")
    if not isinstance(excluded, list) or not excluded:
        return None
    corner = region.get("corner_hz")
    if not isinstance(corner, (int, float)) or isinstance(corner, bool):
        return None
    corner_hz = float(corner)
    return {
        "code": "crossover_region_dip_not_boosted",
        "severity": "info",
        "text": (
            f"A dip near your {corner_hz:.0f} Hz crossover was left un-boosted "
            "on purpose — that's where your subwoofer and speakers hand off, "
            "not a room mode."
        ),
    }


# --------------------------------------------------------------------------
# verdict_text — one plain-language line for the current screen.
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# verdict — the deterministic P4 accept/surface/revert decision (§4 P4).
# --------------------------------------------------------------------------
#
# The evaluator (jasper.correction.acceptance) runs at verify time and lands
# its typed result on ``session.acceptance``. The envelope relays it verbatim
# as a ``verdict`` block and folds a homeowner sentence into the RESULT-screen
# verdict_text. Copy tone is honest: "confirmed improved", "needs another
# look", "reverted — the room says no" (§3.2).
#
# "revert" is deliberately NOT in this map: its copy depends on whether the
# rollback actually ran (session.auto_revert_outcome) — see
# _revert_result_text. A successful revert lands the session in IDLE, so the
# honest success copy lives on the IDLE branch of _verdict_text instead.
_VERDICT_HEADLINE: dict[str, str] = {
    "accept": "Confirmed improved — the room measured better.",
    "surface": "Applied — but the change was too small to be sure. Take a "
    "look and decide.",
    "revert_pending_confirm": "That measured worse. Measure once more to be "
    "sure before we undo it.",
}

# The three truthful revert copies, keyed by what ACTUALLY happened — never
# by intent. Success is only claimed once reset() completed (outcome "ok").
_REVERT_DONE_TEXT = (
    "Reverted — the room says no. The re-measure came back worse, so we "
    "removed the correction and restored your previous sound. You can "
    "measure again anytime."
)
_REVERT_PENDING_ROLLBACK_TEXT = (
    "That measured worse — removing the correction now. If this message "
    "stays, use Reset to remove it."
)


def _revert_outcome(session: Any) -> str | None:
    """The recorded rollback outcome ("ok" / "failed"), or None.

    None means no rollback has run to completion — not attempted yet, or
    still in flight (the session records the outcome when reset() actually
    finishes, so the envelope converges on the truth even after an upload-
    response timeout).
    """
    outcome = getattr(session, "auto_revert_outcome", None)
    if not isinstance(outcome, dict):
        return None
    result = outcome.get("result")
    return result if isinstance(result, str) else None


def _revert_result_text(session: Any) -> str:
    """Result-screen copy for a ``revert`` verdict, driven by the outcome.

    On the result screen a ``revert`` verdict means the rollback has NOT
    completed successfully (success moves the session to IDLE): either it is
    still running (outcome None) or it failed (outcome "failed" — the
    correction is still applied and the copy must say so, with the manual
    Reset pointer). The defensive "ok" branch covers an envelope racing the
    IDLE transition.
    """
    outcome = _revert_outcome(session)
    if outcome == "ok":
        return _REVERT_DONE_TEXT
    if outcome == "failed":
        from .failures import CORRECTION_AUTO_REVERT_FAILED, public_failure

        return str(public_failure(CORRECTION_AUTO_REVERT_FAILED)["text"])
    return _REVERT_PENDING_ROLLBACK_TEXT


def _verdict(session: Any) -> dict[str, Any] | None:
    """Relay the session's deterministic acceptance verdict block.

    ``session.acceptance`` is the dict produced by
    :meth:`MeasurementSession._evaluate_acceptance`
    (``acceptance.AcceptanceResult.to_dict()``): verdict, reasons, the per-band
    table, and the aggregate numbers. Absent until a verify capture lands; a
    malformed value is omitted rather than surfaced half-formed. When an
    automatic rollback has recorded its outcome, it rides along as
    ``auto_revert_outcome`` (additive; the copy is a shallow dict so the
    session's record is never mutated).
    """
    acc = getattr(session, "acceptance", None)
    if not isinstance(acc, dict):
        return None
    verdict = acc.get("verdict")
    if not isinstance(verdict, str):
        return None
    outcome = getattr(session, "auto_revert_outcome", None)
    if isinstance(outcome, dict):
        out = dict(acc)
        out["auto_revert_outcome"] = dict(outcome)
        return out
    return acc


def _crossover_region_note(session: Any) -> str:
    """One homeowner sentence naming the crossover region, or "".

    The design report (strategy.design_correction) carries a ``crossover_region``
    annotation whenever a bass-management corner is being read. On the REVIEW
    screen we fold it into the verdict text so a dip AT the crossover reads as
    "that's your subwoofer handing off, not a room mode" — distinguishing it from
    a genuine room-mode call. Only added when a boost was actually left excluded
    there (otherwise the corner is silent, nothing to explain). Fail-soft: any
    malformed shape yields "".
    """
    report = getattr(session, "design_report", None)
    if not isinstance(report, dict):
        return ""
    region = report.get("crossover_region")
    if not isinstance(region, dict):
        return ""
    excluded = region.get("excluded_boosts")
    if not isinstance(excluded, list) or not excluded:
        return ""
    corner = region.get("corner_hz")
    if not isinstance(corner, (int, float)) or isinstance(corner, bool):
        return ""
    corner_hz = float(corner)
    return (
        f"The dip near {corner_hz:.0f} Hz is where your subwoofer and speakers "
        "hand off — that's the crossover, not a room mode, so we don't boost it."
    )


def _verdict_text(
    session: Any,
    screen: str,
    *,
    blocker: dict[str, Any] | None,
    failure: dict[str, Any] | None,
) -> str:
    """A single homeowner sentence describing where the flow stands.

    Deliberately terse and screen-scoped — the nudges carry the caveats,
    the headline carries the numbers; this is the "what's happening"
    line the dumb frontend shows verbatim.
    """
    if screen == SCREEN_RESULT:
        if session.state.value == "failed":
            return str((failure or {}).get("text") or (
                "The speaker could not continue this step. Try again."
            ))
        if failure is not None:
            return str(failure["text"])
        # The deterministic verdict leads on the result screen — it is the
        # honest "did this work?" answer, ahead of the raw before/after number.
        verdict = _verdict(session)
        if verdict is not None:
            verdict_value = str(verdict.get("verdict"))
            if verdict_value == "revert":
                # Outcome-driven: on this screen the rollback either failed
                # (correction STILL APPLIED — say so, point at Reset) or is
                # still running. Success moved the session to IDLE, whose
                # branch below owns the "we removed it" copy.
                return _revert_result_text(session)
            lead = _VERDICT_HEADLINE.get(verdict_value)
            if lead is not None:
                headline = _headline(session)
                if headline is not None:
                    return f"{lead} {headline['text']}"
                return lead
        headline = _headline(session)
        if headline is not None:
            return f"Done. {headline['text']} You can measure again to compare."
        return "Correction verified."
    if failure is not None:
        return str(failure["text"])
    if screen == SCREEN_APPLY:
        return (
            "Correction is applied. Verify it by measuring once more from "
            "your main seat."
        )
    if screen == SCREEN_REVIEW:
        base = "Here's what your room is doing and the fix we'd apply."
        note = _crossover_region_note(session)
        return f"{base} {note}" if note else base
    if screen == SCREEN_VERIFY:
        return "Measuring again to check the correction worked."
    if screen == SCREEN_SWEEP:
        if session.state.value in {
            "needs_repeat_capture",
            "awaiting_repeat_capture",
        }:
            return (
                "Repeating the main seat once to check that the measurement "
                "is trustworthy."
            )
        total = int(getattr(session, "total_positions", 1) or 1)
        pos = int(getattr(session, "current_position", 0) or 0) + 1
        if total > 1:
            return f"Measuring position {pos} of {total}. Keep the room quiet."
        return "Playing a test sweep. Keep the room quiet."
    if screen == SCREEN_MIC:
        return "Recording a moment of quiet to gauge the room noise."
    if screen == SCREEN_LEVEL:
        if getattr(session, "capture_transport", "local") == "relay":
            level = _level_match_snapshot(session)
            last = level.get("last") if isinstance(level, dict) else None
            ramp = last.get("ramp") if isinstance(last, dict) else None
            state = (
                str(ramp.get("state") or "")
                if isinstance(ramp, dict)
                else ""
            )
        else:
            state = str(_autolevel_snapshot(session).get("status") or "")
        if state == "maxed_out":
            return (
                "The microphone is still too quiet at the safe software limit. "
                "Raise the external amplifier a little, then retry the level check."
            )
        if state in {"error", "cancelled", "aborted"}:
            return "The level check stopped safely. Fix the issue shown, then retry."
        return "Setting a safe, consistent measurement volume."
    # Post-revert IDLE: a successful auto-revert lands here (reset() → IDLE),
    # and "Ready to measure your room." would silently erase what just
    # happened to the household's correction. The session object persists
    # until /start replaces it, so this honest line naturally holds until the
    # user starts a new measurement — the smaller change than inventing a
    # terminal acknowledged-state (no new SessionState, no state-machine or
    # screen-map surgery, no acknowledge endpoint), with the same honesty.
    if _revert_outcome(session) == "ok":
        return _REVERT_DONE_TEXT
    if failure is not None:
        return str(failure["text"])
    if blocker is not None:
        return "Room correction is waiting for speaker setup."
    return "Ready to measure your room."


def _next_action_for(
    session: Any,
    screen: str,
    verdict: dict[str, Any] | None,
    *,
    capture_transport: str,
    relay_capture_pending: bool,
) -> dict[str, str] | None:
    """The single forward button, verdict-aware on the result screen.

    Normally the static ``_NEXT_ACTION`` map. The one override: when the
    deterministic verdict is ``revert_pending_confirm`` on the result screen,
    the forward button becomes the confirmatory re-measure (§4 P4 point 4) —
    ``/verify`` re-runs the one-position verify sweep from the VERIFIED state,
    the concordance gate the auto-revert waits on.

    While a confirmation is pending the envelope deliberately does NOT offer
    ``/start`` as the primary action: /start replaces the session, which
    destroys the pending verdict + concordance state — offering it as the
    default forward button would make the confirmatory gate unreachable via
    the wizard's primary action. /start itself stays available and unblocked
    (measurement flow never blocks): a household that navigates away or
    starts fresh has simply declined — the verdict stays pending, nothing
    reverts, the correction stays applied, and /reset remains the manual
    undo.
    """
    if relay_capture_pending and capture_transport == "relay":
        return None
    if session.state.value == "failed":
        return {
            "label": "Start over",
            "endpoint": "/reset",
        }
    if session.state.value == "needs_next_position":
        return {
            "label": "Measure next position",
            "endpoint": "/next-position",
        }
    if session.state.value == "needs_repeat_capture":
        return {
            "label": "Repeat the main seat",
            "endpoint": (
                "/relay/capture"
                if capture_transport == "relay"
                else "/repeat-position"
            ),
        }
    if (
        screen == SCREEN_MIC
        and getattr(session, "capture_transport", "local") != "relay"
    ):
        if bool(getattr(session, "local_capture_setup_bound", False)):
            return None
        return {
            "label": "Allow microphone",
            "endpoint": "/local-capture/setup",
        }
    if (
        getattr(session, "capture_transport", "local") != "relay"
        and session.state.value == "needs_noise_capture"
        and bool(getattr(session, "local_capture_setup_bound", False))
    ):
        autolevel_status = str(_autolevel_snapshot(session).get("status") or "")
        if screen == SCREEN_LEVEL and autolevel_status == "ramping":
            return None
        if screen == SCREEN_LEVEL and autolevel_status != "locked":
            return {
                "label": (
                    "Retry level check"
                    if autolevel_status in {"cancelled", "error", "maxed_out"}
                    else "Check measurement level"
                ),
                "endpoint": "/autolevel/start",
            }
        return {
            "label": "Measure this position",
            "endpoint": "/upload-noise",
        }
    if getattr(session, "capture_transport", "local") == "relay":
        if session.state.value == "needs_noise_capture":
            level = _level_match_snapshot(session)
            if _relay_level_ready(session):
                return {
                    "label": "Measure this position",
                    "endpoint": "/relay/capture",
                }
            if level.get("running") is True:
                return None
            last = level.get("last") if isinstance(level, dict) else None
            retry = isinstance(last, dict) and last.get("ramp")
            return {
                "label": "Retry level check" if retry else "Check measurement level",
                "endpoint": "/relay/level-match",
            }
        if session.state.value == "applied" or (
            session.state.value == "verified"
            and _relay_confirmation_pending(session)
        ):
            if _relay_level_ready(session):
                return {
                    "label": "Verify the result",
                    "endpoint": "/relay/verify",
                }
            return {
                "label": "Check verification level",
                "endpoint": "/relay/level-match",
            }
    if screen == SCREEN_RESULT and verdict is not None:
        if str(verdict.get("verdict")) == "revert_pending_confirm":
            return {
                "label": "Measure again to confirm",
                "endpoint": "/verify",
            }
    return _NEXT_ACTION.get(screen)


def _run_defaults(
    session: Any,
    *,
    screen: str,
    capture_transport: str,
) -> dict[str, Any]:
    """Disclose the exact Room choices the current or next run will use."""
    from .session import (
        DEFAULT_REPEAT_MAIN_POSITION,
        DEFAULT_ROOM_POSITION_COUNT,
    )
    from .strategy import (
        DEFAULT_CORRECTION_STRATEGY_ID,
        DEFAULT_TARGET_PROFILE_ID,
        resolve_correction_strategy,
        resolve_target_profile,
    )

    total_positions = int(
        getattr(session, "total_positions", DEFAULT_ROOM_POSITION_COUNT)
        or DEFAULT_ROOM_POSITION_COUNT
    )
    target = resolve_target_profile(
        str(getattr(session, "target_choice", DEFAULT_TARGET_PROFILE_ID))
    )
    correction_strategy = resolve_correction_strategy(
        str(
            getattr(
                session,
                "strategy_choice",
                DEFAULT_CORRECTION_STRATEGY_ID,
            )
        )
    )
    repeat_main_position = bool(
        getattr(
            session,
            "repeat_main_position",
            DEFAULT_REPEAT_MAIN_POSITION,
        )
    )
    position_word = "position" if total_positions == 1 else "positions"
    return {
        "summary": (
            f"Measuring {total_positions} {position_word} with the "
            f"{target.label.casefold()} target"
        ),
        "total_positions": total_positions,
        "target": {"id": target.target_id, "label": target.label},
        "strategy": {
            "id": correction_strategy.strategy_id,
            "label": correction_strategy.label,
        },
        "repeat_main_position": repeat_main_position,
        "capture_transport": capture_transport,
        "change_allowed": (
            screen == SCREEN_IDLE and session.state.value == "idle"
        ),
    }


def build_envelope(
    session: Any,
    *,
    capture_transport: str | None = None,
    relay_capture_pending: bool = False,
    reports_available: bool = False,
    readiness_blocker: dict[str, Any] | None | _ReadinessUnset = _READINESS_UNSET,
) -> dict[str, Any]:
    """Build the server-computed screen envelope for one session.

    Pure read over the session; does not mutate it and does not touch the
    ``/status`` payload. Every field is pre-computed for a dumb frontend:
    smoothed curves, the two-tone fill, the one-number headline, the
    deterministic verdict block + verdict text, homeowner nudges, the next
    action, and progress.
    """
    screen = _screen_for(session)
    verdict = _verdict(session)
    transport = str(
        capture_transport
        or getattr(session, "capture_transport", "local")
        or "local"
    )
    next_action = _next_action_for(
        session,
        screen,
        verdict,
        capture_transport=transport,
        relay_capture_pending=relay_capture_pending,
    )
    blocker = None
    if screen == SCREEN_IDLE:
        if isinstance(readiness_blocker, _ReadinessUnset):
            from .failures import (
                ROOM_RETRY_ACTION,
                SPEAKER_READINESS_UNAVAILABLE,
                public_failure,
            )

            blocker = public_failure(
                SPEAKER_READINESS_UNAVAILABLE,
                recovery_action=ROOM_RETRY_ACTION,
            )
        elif readiness_blocker is not None:
            blocker = dict(readiness_blocker)
    if blocker is not None:
        next_action = None
    failure = None
    if session.state.value == "failed":
        from .failures import (
            CORRECTION_AUTO_REVERT_FAILED,
            public_failure,
            session_failure,
        )

        acceptance = getattr(session, "acceptance", None)
        if (
            _revert_outcome(session) == "failed"
            and isinstance(acceptance, dict)
            and acceptance.get("verdict") == "revert"
        ):
            failure = public_failure(CORRECTION_AUTO_REVERT_FAILED)
        else:
            failure = session_failure(getattr(session, "error", None))
    elif screen == SCREEN_REVIEW:
        from .failures import measurement_evidence_failure

        failure = measurement_evidence_failure(
            getattr(session, "confidence_report", None),
        )
    if failure is not None and session.state.value != "failed":
        next_action = None
    tuning_llm = _tuning_llm(screen)
    if failure is not None:
        tuning_llm["offered"] = False
    envelope: dict[str, Any] = {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "screen": screen,
        "state": session.state.value,
        "sections": _sections_for(
            screen,
            capture_transport=transport,
            reports_available=reports_available,
            tuning_offered=bool(tuning_llm.get("offered")),
            readiness_blocked=blocker is not None,
        ),
        "run_defaults": _run_defaults(
            session,
            screen=screen,
            capture_transport=transport,
        ),
        "curves": _curves(session),
        "fill_segments": _fill_segments(session),
        "headline": _headline(session),
        "verdict": verdict,
        "verdict_text": _verdict_text(
            session,
            screen,
            blocker=blocker,
            failure=failure,
        ),
        "nudges": _nudges(session),
        "next_action": dict(next_action) if next_action is not None else None,
        "blocker": blocker,
        "failure": failure,
        "progress": _progress(screen),
        "tuning_llm": tuning_llm,
    }
    return envelope


# Screens where there is a measurement worth explaining, so the "Ask the
# tuning assistant" affordance may show. Pre-measurement screens never
# offer it (nothing to interpret yet).
_TUNING_LLM_SCREENS = frozenset({SCREEN_REVIEW, SCREEN_APPLY, SCREEN_VERIFY, SCREEN_RESULT})


def _tuning_llm(screen: str) -> dict[str, Any]:
    """The P6 tuning-assistant affordance block.

    ``offered`` gates the affordance on a screen with a measurement to
    explain; ``available`` (+ ``nudge`` when False) is the OpenAI-key
    availability from :mod:`jasper.calibration_agent.key_provisioning`.
    The frontend shows the button only when both are true, and shows the
    nudge when offered-but-unavailable. Availability only — no paid call.
    """
    offered = screen in _TUNING_LLM_SCREENS
    from jasper.calibration_agent.key_provisioning import availability

    block = availability().to_dict()
    block["offered"] = offered
    return block


def build_envelope_logged(
    session: Any,
    *,
    capture_transport: str | None = None,
    relay_capture_pending: bool = False,
    reports_available: bool = False,
    readiness_blocker: dict[str, Any] | None | _ReadinessUnset = _READINESS_UNSET,
) -> dict[str, Any]:
    """`build_envelope` plus one structured `event=` line for observability.

    Separate from the pure builder so tests pin the shape without log
    noise; the endpoint calls this variant.
    """
    envelope = build_envelope(
        session,
        capture_transport=capture_transport,
        relay_capture_pending=relay_capture_pending,
        reports_available=reports_available,
        readiness_blocker=readiness_blocker,
    )
    verdict_block = envelope.get("verdict")
    log_event(
        logger,
        "correction_envelope.serve",
        session_id=getattr(session, "session_id", ""),
        screen=envelope["screen"],
        state=envelope["state"],
        sections=",".join(envelope["sections"]),
        nudge_count=len(envelope["nudges"]),
        has_headline=envelope["headline"] is not None,
        verdict=(
            verdict_block.get("verdict")
            if isinstance(verdict_block, dict)
            else None
        ),
        blocker=(
            envelope["blocker"].get("code")
            if isinstance(envelope.get("blocker"), dict)
            else None
        ),
        failure=(
            envelope["failure"].get("code")
            if isinstance(envelope.get("failure"), dict)
            else None
        ),
    )
    return envelope
