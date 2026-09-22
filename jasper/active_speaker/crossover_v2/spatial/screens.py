# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

from jasper.audio_measurement.program_analysis import INTEGRITY_CHECK_SWEEP_HEARD

from ..contracts import CaptureValidity
from ..round_evidence import MeasuredResponse, measured_response_from_analysis
from ..verification import evaluate_capture_validity

if TYPE_CHECKING:
    from jasper.audio_measurement.program_analysis import ProgramAnalysis


# --------------------------------------------------------------------------- #
# the refusal vocabulary — kinds, not household copy
# --------------------------------------------------------------------------- #

#: The stimulus was never located, or located but carried no usable curve.
SCREEN_LOCATE_FAILED = "locate_failed"
#: The two-level pilot pair never cleared the room floor (#1810).
SCREEN_PILOT_LEVEL_COLLAPSE = "pilot_level_collapse"
#: AGC in the recording chain bent the curve being measured.
SCREEN_LINEARITY_FAILED = "linearity_failed"
#: A spliced or otherwise glitched timeline — the transient capture class.
SCREEN_CAPTURE_GLITCH = "capture_glitch"
#: A sweep clipped.
SCREEN_CLIPPED = "clipped"

#: Every kind above, so the flow's mapping can be CHECKED for completeness
#: rather than trusted: a kind added here without an arm there is a wiring
#: defect.
SCREEN_KINDS = frozenset({
    SCREEN_LOCATE_FAILED,
    SCREEN_PILOT_LEVEL_COLLAPSE,
    SCREEN_LINEARITY_FAILED,
    SCREEN_CAPTURE_GLITCH,
    SCREEN_CLIPPED,
})


@dataclass(frozen=True)
class CaptureScreens:
    """The shipped capture-integrity predicates, EVALUATED, for one take.

    Every field is computed by the caller with the shared predicates in
    :mod:`.capture_dispatch`, which are total and side-effect-free, so stating
    them eagerly is exact even though the ladders below short-circuit.

    ``pilot_snr_ok`` and ``linearity_ok`` are tri-state (``None`` = not
    evaluated), and the ladders branch on ``is False``: an unevaluated screen is
    not a failed one.
    """

    stimulus_located: bool
    pilot_snr_ok: bool | None
    linearity_ok: bool | None
    glitch_detected: bool
    sweep_schedule_ok: bool
    any_sweep_clipped: bool


# --------------------------------------------------------------------------- #
# the three ladders
# --------------------------------------------------------------------------- #


def cloud_position_screens(
    screens: CaptureScreens, *, has_summed_response: bool,
) -> str | None:
    """One prompted cloud position: the light per-capture QC, or a refusal kind.

    Per-position work is deliberately light — the group analyses (combine, null
    identification, spec evaluation) run ONCE per group. On the S0 ten-position
    corpus the combine is 2.7-2.8 s and everything layered on it totals
    0.02-0.04 s, so running the set per position would multiply the dominant
    cost by N.

    Two VERIFY gates are deliberately NOT applied, because both assume a
    stationary mic replaying the identical program: gate-comparability (a cloud
    position's gate legitimately differs from the anchor's, since the nearest
    boundary moves with the mic) and the G3 pilot-transfer step (moving the mic
    changes the acoustic transfer by design, so it says nothing about chain
    drift).

    ``has_summed_response`` is the last screen: the stimulus located but no
    summed response came back, so there is no curve to combine.
    """
    if not screens.stimulus_located:
        return SCREEN_LOCATE_FAILED
    if screens.pilot_snr_ok is False:
        # The room/level discriminator runs before the linearity branch so a
        # collapsed pilot pair is never reported as the phone's fault (#1810).
        return SCREEN_PILOT_LEVEL_COLLAPSE
    if screens.linearity_ok is False:
        return SCREEN_LINEARITY_FAILED
    if not has_summed_response:
        return SCREEN_LOCATE_FAILED
    return None


def lateral_pose_screens(screens: CaptureScreens) -> str | None:
    """One pose of the lateral walk, or a refusal kind.

    MEASURE's own capture-integrity gates, in MEASURE's order, because a pose
    replays MEASURE's program. Three MEASURE gates are deliberately NOT applied
    — the delay-search status, the GCC trust floor and the plausibility backstop
    — because all three judge the ALIGNMENT SOLVE, whose search window is a
    geometry prior about the MARK: a microphone 40 cm to the side legitimately
    fails it, and refusing there would keep only the poses that align like the
    anchor.

    A rejected pose does not re-arm MEASURE with a level backoff either: the
    pose must be measured at the ANCHOR'S level or its curve is not comparable.

    The walk's last rung is :func:`lateral_curves_sufficient`.
    """
    if not screens.stimulus_located:
        return SCREEN_LOCATE_FAILED
    if screens.pilot_snr_ok is False:
        return SCREEN_PILOT_LEVEL_COLLAPSE
    if screens.glitch_detected:
        return SCREEN_CAPTURE_GLITCH
    if not screens.sweep_schedule_ok:
        return SCREEN_CAPTURE_GLITCH
    if screens.any_sweep_clipped:
        return SCREEN_CLIPPED
    if screens.linearity_ok is False:
        return SCREEN_LINEARITY_FAILED
    return None


def lateral_curves_sufficient(n_curves: int) -> str | None:
    """The lateral walk's last rung: did this pose yield BOTH branches?

    Fewer than two curves cannot answer a woofer-versus-HF question. Reuses the
    locate kind because the household action is identical.

    A second call rather than an argument to :func:`lateral_pose_screens`
    because counting the curves means BUILDING them, and ``lateral_pose_curve``
    raises ``IndexError`` on a degenerate response with an empty frequency axis.
    Two calls keep the ladder's short-circuit ahead of the builder.
    """
    return SCREEN_LOCATE_FAILED if n_curves < 2 else None


@dataclass(frozen=True)
class EntryBaselineScreen:
    """The entry baseline's verdict: a refusal kind, or the reduced side.

    ``integrity_payload`` is set only on the capture-integrity arm, and is the
    fact the household screen needs beside the code — ``{"capture_integrity":
    ...}``, with an explicit ``None`` inside when the record was ABSENT rather
    than failed.  The other arms carry no payload because the code alone is the
    whole finding.
    """

    kind: str | None
    measured: MeasuredResponse | None = None
    integrity_payload: Mapping[str, Any] | None = None


def entry_baseline_screens(
    analysis: "ProgramAnalysis",
    *,
    stimulus_located: bool,
    reference_mark: str,
) -> EntryBaselineScreen:
    """The "before" capture: screen it, and reduce it when it passes.

    Reuses VERIFY's shipped gates — stimulus locate, ``pilot_snr_ok`` (ahead of
    everything but locate, so a room/level problem is never reported as
    something else, #1810), capture integrity through
    :func:`~.verification.evaluate_capture_validity`, and ``linearity_ok``. One
    deliberate difference: an ABSENT integrity record is UNUSABLE here where
    VERIFY treats it as no-evidence-and-continue, because a before-side nobody
    graded cannot carry a before→after claim.

    Three VERIFY gates are dropped: gate-comparability (it protects an overlay
    this capture never makes), the G3 pilot-transfer step (it protects a
    tracking comparison that does not exist here, and stage 2 may not inherit
    its reference, #1927), and the tracking-max comparison, structurally —
    :func:`~.priors.entry_baseline_priors` withholds ``predicted_sum``.

    One refusal is this phase's own: the reduction
    (:func:`~.round_evidence.measured_response_from_analysis`) must produce a
    side, and ``None`` reuses the locate kind.

    ``analysis`` arrives whole because two steps CONSUME it rather than test it;
    ``stimulus_located`` stays a separate argument because it is a flow-side
    predicate's answer, not an attribute of the analysis.
    """
    if not stimulus_located:
        return EntryBaselineScreen(SCREEN_LOCATE_FAILED)
    if analysis.pilot_snr_ok is False:
        return EntryBaselineScreen(SCREEN_PILOT_LEVEL_COLLAPSE)
    integrity = analysis.capture_integrity
    validity = evaluate_capture_validity(integrity)
    if validity.status is CaptureValidity.UNUSABLE:
        payload = (
            {"capture_integrity": integrity.to_dict()}
            if integrity is not None else {"capture_integrity": None}
        )
        # The same two-code split VERIFY's verdict makes: a sweep nobody could
        # hear is a level/mic problem, a spliced or clipped timeline is the
        # transient glitch class. An ABSENT record takes the glitch kind's
        # silent auto-retry.
        if integrity is not None and INTEGRITY_CHECK_SWEEP_HEARD in integrity.failed:
            return EntryBaselineScreen(SCREEN_LOCATE_FAILED, integrity_payload=payload)
        return EntryBaselineScreen(SCREEN_CAPTURE_GLITCH, integrity_payload=payload)
    if analysis.linearity_ok is False:
        return EntryBaselineScreen(SCREEN_LINEARITY_FAILED)
    measured = measured_response_from_analysis(
        analysis, reference_mark=reference_mark,
    )
    if measured is None:
        return EntryBaselineScreen(SCREEN_LOCATE_FAILED)
    return EntryBaselineScreen(None, measured=measured)
