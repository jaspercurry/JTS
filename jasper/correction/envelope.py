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

This module is **purely additive**. It reads a live
:class:`~jasper.correction.session.MeasurementSession` and derives the
envelope; it does not mutate the session and does not touch the existing
``/status`` payload (:func:`jasper.correction.status.session_snapshot`).
The stepped-wizard page that consumes this is a *later* PR — today the
existing single-page UI keeps rendering from ``/status`` unchanged.

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
from typing import Any

import numpy as np

from jasper.audio_measurement import analysis

from ..log_event import log_event

logger = logging.getLogger(__name__)

# Bumped independently of the bundle schema; a pinning test guards it.
ENVELOPE_SCHEMA_VERSION = 1

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
    reaching into the session. Unknown values fall back to ``idle`` —
    a new backend state should never leave the wizard with no screen.
    """
    return _STATE_SCREEN.get(state_value, SCREEN_IDLE)


def _screen_for(session: Any) -> str:
    """Resolve the live screen, folding in the level-match sub-state.

    The room session has no dedicated "level" state — the §3.1 ramp runs
    while the room session is still IDLE (before the first sweep) and is
    observable only through ``autolevel``. So when the session is IDLE
    but the ramp is actively ramping, the honest screen is "level".
    """
    screen = screen_for_state(session.state.value)
    if screen == SCREEN_IDLE:
        autolevel = _autolevel_snapshot(session)
        if autolevel.get("status") == "ramping":
            return SCREEN_LEVEL
    return screen


def _autolevel_snapshot(session: Any) -> dict[str, Any]:
    al = getattr(session, "autolevel", None)
    if al is None:
        return {}
    try:
        snap = al.snapshot()
    except Exception:  # noqa: BLE001 — never let a bad sub-snapshot break the envelope
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
# sentence. Copy tone (§0.2, §3.2): a sentence + a checkmark, "Continue
# always live", "that's on them" — measurement-quality nudges inform, they
# do not gate. Codes not in this catalog are surfaced generically from the
# finding's own message so a newly-added finding still shows up (degraded
# to its raw wording) rather than vanishing.
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
            "normal for a room. The correction still applies."
        ),
    },
    "high_position_variance": {
        "severity": "warn",
        "text": (
            "Your measured spots differ a lot — often a sign the mic moved "
            "between sweeps or the room is lively. Re-measuring can help, but "
            "you can continue."
        ),
    },
    "capture_snr_low": {
        "severity": "warn",
        "text": (
            "The room was a little noisy during the sweep. Turning it up or "
            "quieting the room improves accuracy — you can still continue."
        ),
    },
    "no_completed_positions": {
        "severity": "warn",
        "text": (
            "No usable measurement yet — run a sweep to see your room's "
            "response."
        ),
    },
}

# A confidence-report finding severity of "fail" is still surfaced as a
# "warn" nudge (never "block") — measurement quality never gates. This
# ceiling maps the report's internal severities onto the nudge vocabulary.
_SEVERITY_CEILING: dict[str, str] = {
    "info": "info",
    "warn": "warn",
    "fail": "warn",
}


def _nudges(session: Any) -> list[dict[str, str]]:
    """Homeowner-language nudges derived from the confidence findings.

    Never a block: the strongest nudge is ``warn``. Order follows the
    confidence report's own finding order (most-impactful first there).
    """
    report = getattr(session, "confidence_report", None)
    if not isinstance(report, dict):
        return []
    findings = report.get("findings")
    if not isinstance(findings, list):
        return []
    nudges: list[dict[str, str]] = []
    seen: set[str] = set()
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        code = str(finding.get("code") or "")
        if not code or code in seen:
            continue
        seen.add(code)
        canned = _NUDGE_COPY.get(code)
        if canned is not None:
            nudges.append(
                {"code": code, "severity": canned["severity"], "text": canned["text"]}
            )
            continue
        # Unknown finding: surface it (degraded to its raw message) rather
        # than dropping it, with severity clamped into the nudge vocabulary.
        raw_sev = str(finding.get("severity") or "info")
        message = str(finding.get("message") or "").strip()
        if not message:
            continue
        nudges.append(
            {
                "code": code,
                "severity": _SEVERITY_CEILING.get(raw_sev, "info"),
                "text": message,
            }
        )
    return nudges


# --------------------------------------------------------------------------
# verdict_text — one plain-language line for the current screen.
# --------------------------------------------------------------------------


def _verdict_text(session: Any, screen: str) -> str:
    """A single homeowner sentence describing where the flow stands.

    Deliberately terse and screen-scoped — the nudges carry the caveats,
    the headline carries the numbers; this is the "what's happening"
    line the dumb frontend shows verbatim.
    """
    if screen == SCREEN_RESULT:
        if session.state.value == "failed":
            err = str(getattr(session, "error", "") or "").strip()
            if err:
                return f"Measurement stopped: {err}"
            return "The measurement stopped before finishing."
        headline = _headline(session)
        if headline is not None:
            return f"Done. {headline['text']} You can measure again to compare."
        return "Correction verified."
    if screen == SCREEN_APPLY:
        return (
            "Correction is applied. Verify it by measuring once more from "
            "your main seat."
        )
    if screen == SCREEN_REVIEW:
        return "Here's what your room is doing and the fix we'd apply."
    if screen == SCREEN_VERIFY:
        return "Measuring again to check the correction worked."
    if screen == SCREEN_SWEEP:
        total = int(getattr(session, "total_positions", 1) or 1)
        pos = int(getattr(session, "current_position", 0) or 0) + 1
        if total > 1:
            return f"Measuring position {pos} of {total}. Keep the room quiet."
        return "Playing a test sweep. Keep the room quiet."
    if screen == SCREEN_MIC:
        return "Recording a moment of quiet to gauge the room noise."
    if screen == SCREEN_LEVEL:
        return "Setting a safe, consistent measurement volume."
    return "Ready to measure your room."


def build_envelope(session: Any) -> dict[str, Any]:
    """Build the server-computed screen envelope for one session.

    Pure read over the session; does not mutate it and does not touch the
    ``/status`` payload. Every field is pre-computed for a dumb frontend:
    smoothed curves, the two-tone fill, the one-number headline, verdict
    text, homeowner nudges, the next action, and progress.
    """
    screen = _screen_for(session)
    next_action = _NEXT_ACTION.get(screen)
    envelope: dict[str, Any] = {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "screen": screen,
        "state": session.state.value,
        "curves": _curves(session),
        "fill_segments": _fill_segments(session),
        "headline": _headline(session),
        "verdict_text": _verdict_text(session, screen),
        "nudges": _nudges(session),
        "next_action": dict(next_action) if next_action is not None else None,
        "progress": _progress(screen),
    }
    return envelope


def build_envelope_logged(session: Any) -> dict[str, Any]:
    """`build_envelope` plus one structured `event=` line for observability.

    Separate from the pure builder so tests pin the shape without log
    noise; the endpoint calls this variant.
    """
    envelope = build_envelope(session)
    log_event(
        logger,
        "correction_envelope.serve",
        session_id=getattr(session, "session_id", ""),
        screen=envelope["screen"],
        state=envelope["state"],
        nudge_count=len(envelope["nudges"]),
        has_headline=envelope["headline"] is not None,
    )
    return envelope
