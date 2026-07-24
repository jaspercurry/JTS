# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The v2 crossover conductor — phase orchestration (Wave 5a).

``docs/crossover-measurement-productization-design.md`` §5 replaces the legacy
per-driver distributed transaction with a **conductor**: the Pi compiles one
excitation program per phase, plays it as one continuous stream, and analyzes
``(program, capture) → analysis`` as a pure function. This module owns the
phase state machine that drives the three-capture relay session:

    CHECK → gain solve → MEASURE → candidate → APPLYING (auto) → VERIFY → done

**Owner ruling (2026-07-20): no human mid-flow Apply gate.** A hardware
session proved the prior REVIEW/APPLY human tap a dead end — phone-only
users cannot bounce to a second browser tab, and "apply this?" is
unanswerable the moment after measuring (the household has no basis to
judge). A trusted candidate (all quality gates pass, including
:data:`ALIGNMENT_CONFIDENCE_TRUST_FLOOR`, promoted here from a review-screen
nudge to a hard gate) is applied by the conductor itself; an untrusted one is
rejected with guidance to re-measure, never a question. See
[docs/HANDOFF-crossover-measurement-v2.md](../../docs/HANDOFF-crossover-measurement-v2.md)
gotcha #18.

It is deliberately I/O-free: every side effect (playback, analysis, evidence
publish, apply-gate observation) crosses an INJECTED seam
(:class:`V2FlowSeams`), exactly as :func:`jasper.active_speaker.program_playback.play_program`
and :class:`jasper.active_speaker.session_volume_plan.SessionVolumePlan` inject
their DSP / volume seams. That keeps the whole state walk fixture-testable with
fake seams, and lets Wave 6 bind the real CamillaController-backed playback, the
``analyze_program_capture`` call, the verified-WAV source, and the
``commissioning_service`` publish/apply chain without touching this logic.

The conductor exposes the three ``run_capture_plan`` callbacks
(:meth:`authorize_begin`, :meth:`on_armed`, :meth:`consume_capture`) plus the
lifecycle hooks the flow needs (:meth:`note_apply_complete`,
:meth:`snapshot`/:meth:`hydrate` for phase persistence + session binding). One
relay session (a 3-entry heterogeneous ``CapturePlan`` — check/measure/verify)
spans all phases; VERIFY is soft-held behind :class:`CaptureBeginDeferred`
until the host's OWN auto-apply completes — the mechanism is unchanged from
the pre-ruling design, only the release trigger moved from a human tap to
:func:`jasper.web.correction_crossover_v2`'s auto-apply hook.

**Failure taxonomy (§5.10).** Terminal verdicts are internal reason codes, not
screens: :data:`REASON_REGISTRY` maps each code to one of the four screen
templates, its owning phase, and its retry budget. The conductor decides the
code + accepted verdict; the envelope (:mod:`jasper.active_speaker.crossover_envelope_v2`)
renders the template. A woofer-repeat level disagreement REUSES
``drift_baselines_disagree`` — never a new user-facing code (§5.2).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from jasper.active_speaker.linearization_envelope import (
    DEFAULT_ENVELOPE_GRID_HZ,
    compose_envelope,
    compute_sigma_curve,
)
from jasper.active_speaker.linearization_fit import (
    complex_correction_response,
    fit_driver_linearization,
)
from jasper.audio_measurement.program import (
    BASE_STIMULUS_PEAK_DBFS,
    DEFAULT_PILOT_LEVELS_DB,
    KIND_SWEEP,
    STIMULUS_KINDS,
    VERIFY_PILOT_ROLE,
    ExcitationProgram,
    RoleBand,
    build_check_program,
    build_measure_program,
    build_verify_program,
)
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_OK,
    FLATNESS_VERIFY_TOLERANCE_DB,
    GainPlan,
    MeasurementGeometry,
    MeasurementPriors,
    ProgramAnalysis,
    overlap_band_hz,
    predicted_branch_sum,
    solve_branch_trims,
    solve_ripple_optimal_trim,
)
from jasper.capture_relay.session import CaptureBeginDeferred, CaptureBeginRefused
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# phase vocabulary
# --------------------------------------------------------------------------- #

PHASE_CHECK = "check"
PHASE_MEASURE = "measure"
# The owner ruling (2026-07-20) removed the human mid-flow Apply gate: a
# trusted candidate is applied by the CONDUCTOR itself, never by a household
# tap. This phase now names the brief machine-paced window between "MEASURE
# accepted" and "apply observed" — the phone sees it as the existing
# CaptureBeginDeferred hold (now captioned "Applying…", not "waiting for the
# household"), and the wizard shows a plain in-progress screen. It is still a
# control-page phase (no capture index) between MEASURE-accepted and
# VERIFY-armed.
PHASE_APPLYING = "applying"
PHASE_VERIFY = "verify"
PHASE_DONE = "done"

# Capture-plan index → phase. APPLYING is a control-page phase (no capture)
# that sits between MEASURE-accepted and VERIFY-armed, so it has no index.
_INDEX_PHASE = {1: PHASE_CHECK, 2: PHASE_MEASURE, 3: PHASE_VERIFY}
_PHASE_INDEX = {phase: index for index, phase in _INDEX_PHASE.items()}
CAPTURE_PLAN_TARGET = 3

# The capturing phases in order — the ones bound to the relay session's
# evidence and invalidated on a new session (§5.6).
CAPTURE_PHASES = (PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY)

# --------------------------------------------------------------------------- #
# failure taxonomy (§5.10)
# --------------------------------------------------------------------------- #

# The four screen templates W5 ships, each parameterized by reason copy.
TEMPLATE_SILENT_AUTO_RETRY = "silent_auto_retry"
TEMPLATE_FIX_AND_RETRY = "fix_and_retry"
TEMPLATE_HARD_STOP = "hard_stop"
TEMPLATE_SESSION_RESTART = "session_restart"
# Two special screens defined in §5.2 (not among the four generic templates).
TEMPLATE_VERIFY_FAIL = "verify_fail"
TEMPLATE_VOLUME_RECOVERY = "volume_recovery"

# Reason codes (internal — never a bare code reaches the household; the envelope
# renders each through its template copy).
REASON_AGC_BEHAVIORAL_FAIL = "agc_behavioral_fail"
# W6.12: the SAME captured-delta-vs-programmed-delta pilot mismatch
# ``REASON_AGC_BEHAVIORAL_FAIL`` names has a second, honest cause hardware
# round 4 proved distinct from the phone's own AGC: a loud ambient burst
# during the pilot pair corrupts the captured level just as effectively, with
# the phone's AGC verifiably off. ``_consume_check`` distinguishes the two
# using the CHECK gain solve's own SNR-floor verdict (``gain_plan.
# snr_floor_ok``, already computed against this exact capture's ambient bands
# independent of the linearity outcome) rather than blaming the phone's
# microphone when the room itself was the problem.
REASON_NOISY_ROOM_LINEARITY = "noisy_room_linearity"
REASON_SNR_FLOOR = "snr_floor"
REASON_CHANNEL_MAP_MISMATCH = "channel_map_mismatch"
REASON_CLIPPED = "clipped"
REASON_DRIFT_BASELINES_DISAGREE = "drift_baselines_disagree"
REASON_DELAY_EXCEEDS_SEARCH_WINDOW = "delay_exceeds_search_window"
REASON_LOCATE_FAILED = "locate_failed"
REASON_RELAY_TIMEOUT = "relay_timeout"
REASON_VOLUME_UNRESOLVED = "volume_unresolved"
# The play seam refused/failed the program (safety re-admission over-cap, a
# graph-restore failure, or a conductor program error) — distinct from a relay
# transport death (``relay_timeout``). After the W6.1 cap-aware composition a
# play-time refusal is unexpected (a bug, a tampered readback, or a genuinely
# infeasible profile), so it is terminal: hard-stop, budget 0.
REASON_PROGRAM_UNPLAYABLE = "program_unplayable"
# Any OTHER host-side fault the session runner's catch-all cleanup arm caught
# (W6.1 gate: the seams raise open-endedly — CamillaUnavailable is a bare
# Exception, analyze/emit raise ValueError/RuntimeError, the held measurement
# window raises MeasurementWindowError — so an enumerated except list is how
# failures escape with the volume active and the phone frozen). Terminal for
# the session; the household's one action is to try again.
REASON_INTERNAL_ERROR = "internal_error"
REASON_VERIFY_OUT_OF_TOLERANCE = "verify_out_of_tolerance"
# Internal-only addition BEYOND the §5.10 table: §5.2's "inconclusive —
# re-verify" verdict (VERIFY's own detected first reflection forced a shorter
# gate than MEASURE's, so the overlay difference is not evidence about driver
# alignment). Renders through the same VERIFY-fail template — it is a distinct
# reason parameterizing that screen's copy, not a fifth screen.
REASON_VERIFY_INCONCLUSIVE = "verify_inconclusive"
# Measurement-honesty gate G3 (2026-07-22): a THIRD, distinct VERIFY-outcome
# reason — the phone's own input chain drifted between VERIFY attempts (see
# VERIFY_PILOT_TRANSFER_STEP_CEILING_DB below for the evidence), not the
# speaker going out of tolerance. Renders through the SAME verify_fail
# template as the two codes above (one more parameterization of that
# screen, not a fifth screen) with its own copy naming the actual cause.
REASON_VERIFY_LEVEL_SHIFT = "verify_level_shift"
# Owner ruling (2026-07-20): the alignment-estimator confidence floor that
# used to gate ONLY a review-screen nudge (informed consent, Apply stayed
# available regardless) is now a hard MEASURE-phase gate — see
# ALIGNMENT_CONFIDENCE_TRUST_FLOOR below. A household has no basis to judge a
# raw confidence number, so doubt becomes guidance ("move the mic"), never a
# question ("apply anyway?").
REASON_LOW_ALIGNMENT_CONFIDENCE = "low_alignment_confidence"
# The conductor's OWN auto-apply (the same transaction a household's tap used
# to trigger) came back blocked or raised — never silently stranding the
# phone on a hold that can only time out dishonestly as relay_timeout.
REASON_APPLY_FAILED = "apply_failed"
# A deliberate phone Stop (CaptureAborted, abort_reason == "stopped") is not a
# relay-transport death — see the catch-all's exception classification in
# jasper.web.correction_crossover_v2. Reuses TEMPLATE_SESSION_RESTART's
# rendering shape (a fresh session is the only way forward either way) with
# honest copy instead of a manufactured "timed out" claim.
REASON_USER_STOPPED = "user_stopped"
# The deferred apply/"review" hold (CaptureBeginDeferred "awaiting_apply")
# expired before the conductor's own auto-apply completed — the apply
# transaction stalled past REVIEW_HOLD_BUDGET_S while the phone waited on the
# hold. Distinct from a relay-transport death (relay_timeout) and a deliberate
# phone Stop (user_stopped): name the actual cause (the apply step timed out)
# rather than a generic "the measurement link timed out" claim (#1605). Same
# TEMPLATE_SESSION_RESTART shape — a fresh session is the only way forward.
REASON_REVIEW_HOLD_TIMEOUT = "review_hold_timeout"


@dataclass(frozen=True)
class ReasonSpec:
    """One terminal verdict's template + budget + copy (§5.10)."""

    code: str
    template: str
    retry_budget: int
    # Short banner shown while a transient code auto-retries (template 1). Empty
    # for codes whose template is a decision screen.
    banner: str
    # The fix/action copy the decision-screen template renders. One reason, one
    # action (the Language guide).
    message: str


# The §5.10 table, as data. The envelope and the conductor both read it, so copy
# and budget never drift between the verdict and its screen.
REASON_REGISTRY: dict[str, ReasonSpec] = {
    REASON_AGC_BEHAVIORAL_FAIL: ReasonSpec(
        REASON_AGC_BEHAVIORAL_FAIL, TEMPLATE_FIX_AND_RETRY, 1, "",
        "Your phone's microphone changed its own levels mid-measurement. "
        "Re-allow the microphone, then try again.",
    ),
    REASON_NOISY_ROOM_LINEARITY: ReasonSpec(
        REASON_NOISY_ROOM_LINEARITY, TEMPLATE_FIX_AND_RETRY, 1, "",
        "The room got loud during that measurement — quiet it and try again.",
    ),
    REASON_SNR_FLOOR: ReasonSpec(
        REASON_SNR_FLOOR, TEMPLATE_FIX_AND_RETRY, 1, "",
        "The room is too loud right now, or the phone is too far away. Quiet "
        "the room or move the phone closer, then try again.",
    ),
    REASON_CHANNEL_MAP_MISMATCH: ReasonSpec(
        REASON_CHANNEL_MAP_MISMATCH, TEMPLATE_HARD_STOP, 0, "",
        # Fix 3 (W6.4): with Fix 1's band-relative discriminator this should
        # be rare and genuinely wiring, but the honest failure mode also
        # includes a very quiet/noisy room (the discriminator needs both a
        # driver's own band to rise over its ambient AND the other driver's
        # band to stay quiet) — name both causes rather than blaming wiring
        # unconditionally.
        "The drivers didn't play in the expected order — check the speaker "
        "wiring, or if the room is noisy, quiet it and try again.",
    ),
    REASON_CLIPPED: ReasonSpec(
        REASON_CLIPPED, TEMPLATE_SILENT_AUTO_RETRY, 1,
        "That was a touch loud — measuring again a bit quieter.", "",
    ),
    REASON_DRIFT_BASELINES_DISAGREE: ReasonSpec(
        REASON_DRIFT_BASELINES_DISAGREE, TEMPLATE_SILENT_AUTO_RETRY, 1,
        "The capture glitched — measuring again.", "",
    ),
    REASON_DELAY_EXCEEDS_SEARCH_WINDOW: ReasonSpec(
        REASON_DELAY_EXCEEDS_SEARCH_WINDOW, TEMPLATE_FIX_AND_RETRY, 1, "",
        "The microphone may be off the spot in the picture. Re-check its "
        "placement, then try again.",
    ),
    REASON_LOCATE_FAILED: ReasonSpec(
        REASON_LOCATE_FAILED, TEMPLATE_FIX_AND_RETRY, 1, "",
        "Couldn't hear the speaker clearly. Check the volume and the "
        "microphone, then try again.",
    ),
    REASON_RELAY_TIMEOUT: ReasonSpec(
        REASON_RELAY_TIMEOUT, TEMPLATE_SESSION_RESTART, 0, "",
        # The old link is dead once the session collapses — do NOT tell the
        # household to "open the link again" (W6.10 fold-in: that link and its
        # QR are gone). Start over mints a FRESH session from this page.
        "The measurement link timed out. Start over from this page to measure "
        "again — the quick microphone check runs first.",
    ),
    REASON_VOLUME_UNRESOLVED: ReasonSpec(
        REASON_VOLUME_UNRESOLVED, TEMPLATE_VOLUME_RECOVERY, 0, "",
        "JTS could not confirm the listening volume was restored. Recover the "
        "safe volume before continuing.",
    ),
    REASON_PROGRAM_UNPLAYABLE: ReasonSpec(
        REASON_PROGRAM_UNPLAYABLE, TEMPLATE_HARD_STOP, 0, "",
        "JTS could not play the measurement signal within the speaker's safe "
        "limits. Re-check the driver details in speaker setup, then measure "
        "again.",
    ),
    REASON_INTERNAL_ERROR: ReasonSpec(
        REASON_INTERNAL_ERROR, TEMPLATE_FIX_AND_RETRY, 0, "",
        "Something went wrong on the speaker during that measurement. "
        "Try again.",
    ),
    REASON_VERIFY_OUT_OF_TOLERANCE: ReasonSpec(
        REASON_VERIFY_OUT_OF_TOLERANCE, TEMPLATE_VERIFY_FAIL, 2, "",
        "The result didn't quite match the prediction. Try again, or undo to "
        "restore the previous sound.",
    ),
    REASON_VERIFY_INCONCLUSIVE: ReasonSpec(
        REASON_VERIFY_INCONCLUSIVE, TEMPLATE_VERIFY_FAIL, 2, "",
        "The check was inconclusive — the room reflection cut the window "
        "short. Re-verify to try again.",
    ),
    REASON_VERIFY_LEVEL_SHIFT: ReasonSpec(
        REASON_VERIFY_LEVEL_SHIFT, TEMPLATE_VERIFY_FAIL, 2, "",
        "Your phone's microphone levels changed between measurements — "
        "re-verify to try again.",
    ),
    REASON_LOW_ALIGNMENT_CONFIDENCE: ReasonSpec(
        REASON_LOW_ALIGNMENT_CONFIDENCE, TEMPLATE_FIX_AND_RETRY, 1, "",
        "Alignment is less certain at this mic position. Place the microphone "
        "about 1 m in front of the speaker at tweeter height, then measure "
        "again.",
    ),
    REASON_APPLY_FAILED: ReasonSpec(
        REASON_APPLY_FAILED, TEMPLATE_FIX_AND_RETRY, 1, "",
        "JTS could not apply the measured crossover automatically. Try again.",
    ),
    REASON_USER_STOPPED: ReasonSpec(
        REASON_USER_STOPPED, TEMPLATE_SESSION_RESTART, 0, "",
        "You stopped the measurement. Start over from this page when you're "
        "ready.",
    ),
    REASON_REVIEW_HOLD_TIMEOUT: ReasonSpec(
        REASON_REVIEW_HOLD_TIMEOUT, TEMPLATE_SESSION_RESTART, 0, "",
        "Applying the measured crossover took too long, so the measurement "
        "timed out before it could finish. Start over from this page to "
        "measure again — the quick microphone check runs first.",
    ),
}

# The transient codes whose first retry is automatic (a banner, no decision
# screen) per §5.10 template 1.
TRANSIENT_AUTO_RETRY_CODES = frozenset(
    code for code, spec in REASON_REGISTRY.items()
    if spec.template == TEMPLATE_SILENT_AUTO_RETRY
)

# --------------------------------------------------------------------------- #
# tuning constants (PROVISIONAL pending W6 bench validation)
# --------------------------------------------------------------------------- #

# The gain solver backs off this far below each driver's exact cap. The W2 gate
# found ``prepare_driver_excitation_plan``'s strict ``>`` can refuse an
# exactly-at-cap plan by one ulp, so a hair of headroom keeps an at-cap solve
# admissible.
GAIN_CAP_BACKOFF_DB = 0.01
# Per gain-adjusted clip retry, drop the offending program's level by this much.
CLIP_RETRY_BACKOFF_DB = 3.0
# The two pilot levels are this far apart (matches the CHECK behavioral check).
PILOT_LEVEL_DELTA_DB = abs(DEFAULT_PILOT_LEVELS_DB[1] - DEFAULT_PILOT_LEVELS_DB[0])
# A located stimulus below this correlation confidence reads as "couldn't hear
# the speaker" (locate_failed).
LOCATE_MIN_CONFIDENCE = 0.1
# VERIFY PASS: |measured sum − predicted sum| ≤ this over [Fc/2, 2·Fc] (§5.2),
# measured against the notch-excluded max (W6.7 ruling 1 —
# `program_analysis.VERIFY_NOTCH_EXCLUSION_DB`) rather than the raw max.
VERIFY_TOLERANCE_DB = 1.5
# The prescribed on-axis mic distance the parallax correction assumes (§5.2).
MEASUREMENT_DISTANCE_M = 1.0
# Below this GCC-seed/capture confidence (see ``AlignmentEstimate.confidence``
# and ``confidence_source`` in ``program_analysis.py``), the conductor refuses
# to auto-apply and rejects
# MEASURE with ``REASON_LOW_ALIGNMENT_CONFIDENCE`` instead of building a
# candidate (owner ruling, 2026-07-20). Formerly
# ``crossover_envelope_v2.ALIGNMENT_CONFIDENCE_NUDGE_FLOOR`` — a review-screen
# nudge that left Apply available regardless ("informed consent, not a
# gate"). Moved here and promoted to a hard gate now that apply is automatic:
# there is no more human screen to hand the informed-consent judgment to.
# PROVISIONAL pending W6 bench distributions on confidence-vs-outcome
# correlation (unchanged from the prior nudge floor's own provisional status).
ALIGNMENT_CONFIDENCE_TRUST_FLOOR = 0.6
# Physical-plausibility backstop (Fix 3, 2026-07-21): the GCC estimator can
# return a CONFIDENTLY WRONG delay (a hardware run reported a confident
# −631 us against this preset's declared [50, 300] us delay_range_ms search
# bound) that still clears ALIGNMENT_CONFIDENCE_TRUST_FLOOR above — high GCC
# correlation confidence at the wrong lag is a real failure mode, not a
# hypothetical one. This margin is added on BOTH sides of the crossover
# region's declared ``delay_range_ms`` (a SEARCH bound per
# ``jasper.active_speaker.profile.CrossoverRegion``'s own docstring, not a
# hard physical limit) before a measured delay outside it is rejected, so a
# delay a little past the declared bound isn't treated the same as one
# wildly outside it. PROVISIONAL pending W6 bench validation, same status as
# the confidence floor above.
ALIGNMENT_DELAY_PLAUSIBILITY_MARGIN_MS = 0.1

# Measurement-honesty gate G1 (2026-07-22): a corrupted phone-chain MEASURE
# capture on 2026-07-22 hardware built a candidate whose ``predicted_ripple_db``
# was 27.316 dB at an alignment confidence (0.703) that cleared
# ALIGNMENT_CONFIDENCE_TRUST_FLOOR above — the candidate auto-applied, then
# failed three VERIFYs at 5.3-6.7 dB. Every clean MEASURE that same day (13
# captures across UMIK-2, iMM-6C, and the phone chain) predicted
# 4.387-9.031 dB. This ceiling sits ~6 dB
# above the clean corpus's worst case and ~12 dB below the corrupt one — wide
# margin on both sides. A candidate whose OWN predicted ripple is this bad is
# not a trustworthy basis for auto-apply regardless of what alignment
# confidence reported, so this REUSES REASON_LOW_ALIGNMENT_CONFIDENCE (same
# household action — "measure again" — as the confidence floor and Fix 3's
# plausibility backstop above; the diag ``guard`` field disambiguates which of
# the three actually fired in telemetry). PROVISIONAL pending W6 bench
# validation, same status as every other MEASURE-phase gate in this block.
MEASURE_PREDICTED_RIPPLE_CEILING_DB = 15.0

# Measurement-honesty gate G2 (2026-07-22): an ``event=outputd.xrun`` playback
# glitch on 2026-07-22 hardware shifted a MEASURE capture's three sweeps
# −25…−28 ms off their SCHEDULED slot with per-segment locate confidence
# 0.07-0.12 (the measured clean corpus's WORST capture ran ≤1.5 ms residual
# at ≥0.6926 confidence) while ``glitch_detected`` stayed False — the
# repeat-pair drift check (``_estimate_drift``) is structurally blind to a
# uniform whole-capture shift (its own residual guard demeans per role, so
# it only catches a WITHIN-driver desync), and ``_stimulus_locate_ok`` passes
# on the max() confidence across every located stimulus, so one good segment
# masks three bad sweeps. Both thresholds carry wide margin on both sides of
# the two clusters above. PROVISIONAL pending W6 bench validation.
SWEEP_SCHEDULE_RESIDUAL_CEILING_MS = 5.0
SWEEP_LOCATE_CONFIDENCE_FLOOR = 0.3

# Measurement-honesty gate G3 (2026-07-22): the gate's OWN metric (summed-
# pilot transfer step) measured the phone's input chain stepping 0.75-0.82
# dB across the dishonest 1.192 → 2.111 → 2.835 dB VERIFY attempt sequence on
# 2026-07-22 hardware, producing verdicts that read as "speaker out of
# tolerance" when the recorder was what changed — the one clean multi-
# attempt session on the same rig stepped ≤0.05 dB by that SAME metric. (A
# separate, coarser frequency-differential estimate of the same drift put it
# at ~0.56 dB — kept only as secondary corroborating context; the pilot-band
# numbers above are what this gate actually measures and are the primary
# evidence.) VERIFY replays the IDENTICAL program through the IDENTICAL
# applied graph on every attempt, so its own leading pilot pair's transfer
# (captured level minus programmed gain) should not move between attempts
# either — a step this large is the input chain moving, not the speaker.
# PROVISIONAL pending W6 bench validation.
VERIFY_PILOT_TRANSFER_STEP_CEILING_DB = 0.35

# Pre-capture courtesy tone (issue #1677): default ON, no env/config switch.
# The owner's live-incident report (a headless session's first sweep started
# while music was playing, forcing a void + re-run) plus the house
# "no-silent-failure" / "no speculative flexibility" rules both point the
# same way — every household benefits from the warning, and there is no
# stated case for wanting it off. Every ``build_v2_capture_plan`` /
# ``build_v2_verify_capture_plan`` (phone duration budget) and conductor
# ``_compose_*_program`` (actual playback) call in this module passes this
# SAME constant, so the two can never disagree about whether the prelude is
# present — the phone would otherwise budget a shorter recording window than
# the program it's actually capturing (see the ``+3.6 s`` proof in
# ``test_crossover_v2_conductor.py``, mirroring PR-A's ``+15 s`` MEASURE
# lengthening). ``jasper.audio_measurement.program``'s own composers default
# ``courtesy_prelude`` to ``False`` so every OTHER caller (tests, future
# tools) keeps today's byte-identical shape unless it opts in explicitly.
COURTESY_PRELUDE_ENABLED = True


class CrossoverV2FlowError(RuntimeError):
    """The v2 conductor could not form a safe phase transition."""


# --------------------------------------------------------------------------- #
# pure helpers (fixture-testable in isolation)
# --------------------------------------------------------------------------- #


def back_off_gain(gain_db: float, session_volume_db: float, cap_dbfs: float,
                  *, margin_db: float = GAIN_CAP_BACKOFF_DB) -> float:
    """Clamp a per-driver digital gain so its effective peak stays under the cap.

    The effective peak folded through the session volume is
    ``gain_db + session_volume_db``; admission caps it at the driver's
    ``cap_dbfs``. The W2 gate found the admission's strict ``>`` can refuse an
    exactly-at-cap plan by one ulp, so this backs off ``margin_db`` (≥0.01 dB)
    below the cap — an at-cap solve stays admissible.
    """
    ceiling = cap_dbfs - session_volume_db - margin_db
    return min(float(gain_db), ceiling)


def alignment_to_candidate_fields(
    analysis: ProgramAnalysis, *, woofer_role: str, tweeter_role: str,
) -> tuple[float | None, str | None, str | None]:
    """Map a MEASURE ``AlignmentEstimate`` to ``(delay_us, delay_role, polarity)``.

    Honours the analysis sign contract (design §5.6.5): its ``delay_us`` is
    ``(D_woofer − D_tweeter)``, so **positive ⇒ the tweeter arrived earlier and
    the tweeter branch is delayed**; negative ⇒ the woofer is delayed. The W4
    :class:`~jasper.active_speaker.measured_crossover_candidate.MeasuredCrossoverAlignment`
    wants a non-negative magnitude + the delayed role, so the sign is folded into
    the role choice. Returns ``(None, None, None)`` when there is no trustworthy
    alignment (missing, or the estimator clamped at the search-window edge), so
    the candidate falls back to a trims-only apply.
    """
    from jasper.active_speaker.crossover_alignment import (
        POLARITY_INVERT,
        POLARITY_KEEP,
    )

    est = analysis.alignment
    if est is None or est.status != ALIGNMENT_OK:
        return None, None, None
    delay_us = float(est.delay_us)
    if delay_us >= 0.0:
        role, magnitude = tweeter_role, delay_us
    else:
        role, magnitude = woofer_role, -delay_us
    polarity = POLARITY_INVERT if est.polarity == "inverted" else POLARITY_KEEP
    return magnitude, role, polarity


def _declared_alignment_delay_range_ms(
    source_preset: Any,
) -> tuple[Any, float, float] | None:
    """Return the single v2 region plus its valid declared delay range."""
    regions = getattr(source_preset, "crossover_regions", None)
    if not regions:
        return None
    region = regions[0]
    delay_range_ms = getattr(region, "delay_range_ms", None)
    if not (isinstance(delay_range_ms, (tuple, list)) and len(delay_range_ms) == 2):
        return None
    lo_ms, hi_ms = float(delay_range_ms[0]), float(delay_range_ms[1])
    if not (math.isfinite(lo_ms) and math.isfinite(hi_ms)) or lo_ms > hi_ms:
        return None
    return region, lo_ms, hi_ms


def alignment_delay_search_bounds_us(
    source_preset: Any,
    *,
    margin_ms: float = ALIGNMENT_DELAY_PLAUSIBILITY_MARGIN_MS,
) -> tuple[float, float] | None:
    """Flatness-search magnitude bounds from the preset's declaration.

    The range and margin are the same ones Fix 3's plausibility gate reads.
    ``delay_target_driver`` is optional until a delay has actually been applied,
    so it cannot orient a fresh measurement. The analysis uses the
    drift-corrected physical peak gap to orient and center one signed lobe
    inside these declared magnitude bounds; GCC remains confidence, polarity,
    and fallback evidence only.
    """
    declared = _declared_alignment_delay_range_ms(source_preset)
    if declared is None:
        return None
    _region, lo_ms, hi_ms = declared
    lo_ms = max(0.0, lo_ms - margin_ms)
    hi_ms += margin_ms
    return lo_ms * 1000.0, hi_ms * 1000.0


def alignment_delay_plausible(
    delay_us: float | None,
    source_preset: Any,
    *,
    margin_ms: float = ALIGNMENT_DELAY_PLAUSIBILITY_MARGIN_MS,
) -> bool:
    """True when ``|delay_us|`` falls inside the preset's declared crossover
    region ``delay_range_ms`` search bound (± ``margin_ms``), or when there is
    no declared bound / no delay to judge (nothing to gate on).

    Physical-plausibility backstop (Fix 3): see
    :data:`ALIGNMENT_DELAY_PLAUSIBILITY_MARGIN_MS`. Declaration-driven —
    reads the SAME ``delay_range_ms`` the crossover region already carries as
    a search bound (:class:`jasper.active_speaker.profile.CrossoverRegion`),
    never a hardcoded delay literal. The v2 conductor is scoped to a single
    2-way crossover region (``crossover_regions[0]``), matching every other
    single-region read in this module (e.g. ``resolve_conductor_context``).
    """
    if delay_us is None:
        return True
    declared = _declared_alignment_delay_range_ms(source_preset)
    if declared is None:
        return True
    _region, lo_ms, hi_ms = declared
    delay_ms = abs(float(delay_us)) / 1000.0
    return (lo_ms - margin_ms) <= delay_ms <= (hi_ms + margin_ms)


def _analysis_json(analysis: ProgramAnalysis) -> dict[str, Any]:
    """Compact JSON-safe evidence core for the measured candidate fingerprint.

    The W4 candidate freezes ``analysis`` as exact JSON data, so only the
    scalar verdicts travel — never the numpy response arrays. Enough to identify
    the exact measurement that authorized the candidate (§5.6/§5.8).
    """
    drift = analysis.drift
    align = analysis.alignment
    cand = analysis.candidate
    return {
        "schema_version": 1,
        "kind": "jts_program_analysis_evidence",
        "program_id": analysis.program_id,
        "epsilon_ppm": round(float(drift.epsilon_ppm), 3) if drift else None,
        "glitch_detected": bool(analysis.glitch_detected),
        "delay_us": round(float(align.delay_us), 3) if align else None,
        "alignment_seed_delay_us": (
            round(float(align.seed_delay_us), 3)
            if align and align.seed_delay_us is not None else None
        ),
        "polarity": align.polarity if align else None,
        "alignment_confidence": round(float(align.confidence), 4) if align else None,
        "alignment_confidence_source": align.confidence_source if align else None,
        "trim_db": (
            {k: round(float(v), 4) for k, v in cand.trim_db.items()} if cand else None
        ),
        # #1667: the band-average seed trim_db's ripple-optimal solve started
        # from — evidence only, so replay/forensics can always see both even
        # when the applied trim_db above coincides with it (the sanity-guard
        # fallback path).
        "trim_band_average_db": (
            {k: round(float(v), 4) for k, v in cand.trim_band_average_db.items()}
            if cand and cand.trim_band_average_db is not None else None
        ),
        "predicted_ripple_db": (
            round(float(cand.predicted_ripple_db), 4) if cand else None
        ),
        "alignment_seed_ripple_db": (
            round(float(cand.alignment_seed_ripple_db), 4)
            if cand and cand.alignment_seed_ripple_db is not None else None
        ),
        "flatness_improvement_db": (
            round(float(cand.flatness_improvement_db), 4)
            if cand and cand.flatness_improvement_db is not None else None
        ),
        "anchor_delay_us": (
            round(float(cand.anchor_delay_us), 3)
            if cand and cand.anchor_delay_us is not None else None
        ),
        "snap_delta_us": (
            round(float(cand.snap_delta_us), 3)
            if cand and cand.snap_delta_us is not None else None
        ),
        "snap_found": bool(cand.snap_found) if cand else None,
    }


def _stimulus_locate_ok(analysis: ProgramAnalysis) -> bool:
    """False when no located stimulus cleared the locate-confidence floor."""
    confidences = [
        loc.confidence for loc in analysis.locations if loc.kind in STIMULUS_KINDS
    ]
    if not confidences:
        return False
    return max(confidences) >= LOCATE_MIN_CONFIDENCE


def _sweep_schedule_ok(analysis: ProgramAnalysis, sample_rate_hz: int) -> bool:
    """False when a MEASURE sweep landed off its scheduled slot, or was only
    weakly located (measurement-honesty gate G2, 2026-07-22 — the xrun
    detector; see :data:`SWEEP_SCHEDULE_RESIDUAL_CEILING_MS` for the evidence).

    ``sample_rate_hz`` is deliberately the CALLER's own MEASURE program rate,
    not something read off ``analysis`` itself:
    ``analyze_program_capture`` HARD-REFUSES a capture whose sample rate
    disagrees with the program's own (``capture rate != program rate``,
    ``jasper.audio_measurement.program_analysis``), and the relay capture
    spec fixes every phone upload at ``REQUIRED_SAMPLE_RATE_HZ`` (48 kHz,
    ``jasper.capture_relay.spec``) — so no resampling ever runs between the
    phone's WAV and this analysis, and ``SegmentLocation.residual_samples``
    is always expressed in exactly that domain (the conductor's own composed
    program's ``sample_rate_hz``).

    Filtered to ``KIND_SWEEP`` only — mirrors ``_estimate_drift``'s exclusion
    of the leading pilot pair from residual/drift logic (their short/quiet
    windows locate more coarsely and would manufacture spurious fires here).
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
        if loc.confidence < SWEEP_LOCATE_CONFIDENCE_FLOOR:
            return False
    return True


def _sweep_schedule_diag_fields(
    analysis: ProgramAnalysis, sample_rate_hz: int,
) -> tuple[float | None, float | None]:
    """``(sweep_residual_ms_worst, sweep_locate_confidence_min)`` — diagnostic
    only, over the SAME ``KIND_SWEEP`` domain ``_sweep_schedule_ok`` gates on,
    but never itself gates a verdict. ``sweep_residual_ms_worst`` is the
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


def _any_sweep_clipped(analysis: ProgramAnalysis) -> bool:
    return any(
        loc.clipped for loc in analysis.locations if loc.kind in STIMULUS_KINDS
    )


def _gate_window_ms(response: Any) -> float | None:
    if response is None:
        return None
    window = response.gating.get("window_ms") if response.gating else None
    return float(window) if isinstance(window, (int, float)) else None


def _verify_evidence_from_tracking(
    tracking: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The verify_fail expert-disclosure numbers (#1605): the notch-excluded
    max the tolerance gates on, the RMS, the tracking band, and the tolerance
    itself. Returns None when the gated max is not a real number — nothing
    meaningful to show behind the disclosure."""
    max_db = tracking.get("max_db_notch_excluded")
    if not isinstance(max_db, (int, float)):
        return None
    rms_db = tracking.get("rms_db")
    band = tracking.get("tracking_band_hz")
    lo = hi = None
    if isinstance(band, (list, tuple)) and len(band) == 2:
        lo, hi = band
    return {
        "max_db": float(max_db),
        "rms_db": float(rms_db) if isinstance(rms_db, (int, float)) else None,
        "tracking_band_lo_hz": float(lo) if isinstance(lo, (int, float)) else None,
        "tracking_band_hi_hz": float(hi) if isinstance(hi, (int, float)) else None,
        "tolerance_db": float(VERIFY_TOLERANCE_DB),
    }


# --------------------------------------------------------------------------- #
# diagnostic-logging helpers (Part 1 — additive; feed no verdict)
# --------------------------------------------------------------------------- #
#
# Every CHECK/MEASURE/VERIFY capture logs its full numeric diagnostics on
# PASS *and* FAIL via ``log_event`` — previously only ``program_analysis.
# glitch`` carried a partial view (epsilon/residual/repeat-level, WARN-only,
# glitch captures only) and the ``crossover_v2_result`` line carried just the
# reason code, so a failed hardware run left no numbers to look at. These
# helpers read what ``ProgramAnalysis`` already computed; none of them derive
# a NEW number or influence any verdict.


def _driver_response_by_role(analysis: ProgramAnalysis, role: str) -> Any | None:
    for resp in analysis.driver_responses:
        if resp.role == role:
            return resp
    return None


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
    for TWO independent reasons: (1) it is a RELATIVE cross-ATTEMPT
    comparison (this attempt's transfer minus the FIRST attempt's), never a
    true absolute-level read; and (2) a v2 MEASURE/VERIFY leading pilot pair
    is built with NO ambient window at all (``program_analysis._pilot_verdicts``'s
    docstring: "a MEASURE/VERIFY pilot pair has no leading ambient window of
    its own"), so ``_pilot_observations`` degrades ambient subtraction to a
    no-op for every VERIFY pilot today — ``level_hi_dbfs`` here is the plain
    band-relative in-band RMS level, not an ambient-adjusted one. If VERIFY's
    leading pilot pair is ever given an ambient window in the future, this
    gate needs to be revisited: two attempts observed against DIFFERENT
    ambient levels would inject an ambient-difference confound into a step
    that is supposed to isolate the recording chain's own drift.
    """
    return {
        pilot.role: pilot.level_hi_dbfs - pilot.programmed_hi_gain_db
        for pilot in analysis.pilots
        if pilot.programmed_hi_gain_db is not None
    }


def _driver_snr_fields(resp: Any | None) -> tuple[float | None, str | None]:
    """``(estimated_snr_db, verdict)`` from a driver's worst-relevant SNR band."""
    if resp is None or resp.snr is None:
        return None, None
    worst = resp.snr.get("worst_relevant") or {}
    return worst.get("estimated_snr_db"), worst.get("verdict")


def _measure_validity_floor_hz(analysis: ProgramAnalysis) -> float | None:
    """The worse (higher) of the two driver responses' own reflection-gate floor.

    Mirrors ``_build_candidate``'s ``branch_floor_hz`` clamp — diagnostic
    only here, does not feed any verdict in this module.
    """
    floors = [
        r.validity_floor_hz for r in analysis.driver_responses
        if r.validity_floor_hz is not None
    ]
    return max(floors) if floors else None


def _pilot_diag_fields(pilot: Any | None) -> dict[str, float | None]:
    """One pilot's linearity/SNR/channel-map diagnostics, ``None``-safe."""
    if pilot is None:
        return {
            "snr_db": None,
            "captured_delta_db": None,
            "programmed_delta_db": None,
            "channel_map_target_rise_db": None,
            "channel_map_cross_rise_db": None,
        }
    snr_db = pilot.snr_db
    target_rise = pilot.channel_map_target_rise_db
    cross_rise = pilot.channel_map_cross_rise_db
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
    }


# --------------------------------------------------------------------------- #
# Layer-1a driver-linearization wiring (#1668 PR-C)
# --------------------------------------------------------------------------- #
#
# The fit engine (jasper.active_speaker.linearization_fit) and the envelope
# core (jasper.active_speaker.linearization_envelope) are pure, policy-free
# computation. This conductor is where their outputs become a PRODUCT
# decision: gate eligibility (mic tier + paired repeat count), σ-composition
# policy, and the trim re-solve + sanity backstop. See
# docs/active-speaker-tuning-layers-design.md "Layer 1a concretely".

# Both drivers of the pair must carry at least this many in-capture
# occurrences (primary + repeats) before Layer-1a trusts ANY repeatability
# evidence — the "paired gate" (sigma-seeding report finding 5: "don't trust
# live sigma alone until N>=3 for BOTH drivers"). Mirrors the v2 MEASURE
# program's own default repeat count
# (jasper.audio_measurement.program.MEASURE_REPEAT_COUNT) — not imported,
# since this is a POLICY floor (what linearization requires), not a
# statement about what the program composes; the two happen to agree today.
LINEARIZATION_MIN_PAIRED_OCCURRENCES = 3

# How far the trim re-solved from the LINEARIZED branch responses may move
# from the raw (unlinearized) trim before it is treated as implausible and
# discarded in favor of the raw value (with a WARNING — never a silent
# swap). A correction that is honoring its own envelope caps (<=12 dB cut
# per bin, <=6 dB total normalization spend) cannot plausibly move a
# BAND-AVERAGE trim this far; a bigger swing means something upstream (a
# bad calibration, a badly time-aligned response) fed the re-solve garbage.
LINEARIZATION_TRIM_SANITY_MARGIN_DB = 6.0

# Mirrors jasper.active_speaker.linearization_envelope._SIGMA_TOLERABLE_DB
# (module-private there — see that module's top docstring for the "no
# cross-module private imports" convention this repo follows). LOCKSTEP
# REQUIREMENT: any change to that table must be mirrored here, or this
# conductor's sigma floor and the envelope module's own
# repeatability_limit() disagree about what "tolerable" means per tier.
_SIGMA_TOLERABLE_DB: Mapping[str, float] = {
    "reference": 0.5,
    "consumer": 1.0,
    "phone": 1.5,
}


def _compose_sigma_db(
    own: Any,
    sibling: Any,
    *,
    tier: str,
    valid_band_hz: tuple[float, float],
    grid_hz: np.ndarray = DEFAULT_ENVELOPE_GRID_HZ,
) -> np.ndarray | None:
    """PR-C's σ-composition policy: the paired-N gate + the per-tier floor.

    ``own``/``sibling`` are the two :class:`~jasper.audio_measurement.
    program_analysis.DriverResponse` of a crossover pair (typed ``Any`` —
    matching this module's own convention of not importing program_analysis
    dataclasses purely for type hints). Returns ``None`` (no evidence, no
    permission — the same contract
    :func:`~jasper.active_speaker.linearization_envelope.compute_sigma_curve`
    itself uses) when EITHER driver has fewer than
    :data:`LINEARIZATION_MIN_PAIRED_OCCURRENCES` occurrences (primary +
    repeats) — an under-repeated sibling voids the pair's trust even if
    ``own`` alone has plenty. This gate is deliberately redundant with the
    conductor's own outer eligibility gate (:meth:`CrossoverV2Conductor.
    _linearization_eligible`) — belt-and-suspenders, so this function stays
    independently correct/safe if ever called from a different context.

    Otherwise computes ``own``'s live σ(f)
    (:func:`~jasper.active_speaker.linearization_envelope.compute_sigma_curve`)
    and floors it at the tier's own tolerable value:
    ``sigma_eff = max(sigma_tolerable(tier), live)``.

    **This floor is currently BEHAVIORALLY INERT.** ``repeatability_limit``'s
    own formula is ``D_cap * min(1, sigma_tolerable / max(sigma, eps))`` —
    for ANY ``live <= sigma_tolerable`` that expression already saturates at
    ``D_cap * 1`` (the full ceiling), identically whether ``live`` is floored
    up to ``sigma_tolerable`` or left alone. Flooring at EXACTLY the tier's
    own tolerable value therefore changes nothing about the resulting
    envelope today; it exists as a SEAM for a future PR that might set the
    floor HIGHER than ``sigma_tolerable`` for genuine extra conservatism
    (e.g. a stricter product-taste floor independent of the envelope
    module's own per-tier table). Do not assume this floor currently does
    more than the paired-N gate above.

    N2 (2026-07-24 adversarial review): flagged this same inertness;
    coordinator ruling was to KEEP it as-is — it is the σ-seeding report's
    own recommended composition, already honestly documented here and
    pinned by ``test_compose_sigma_db_floor_is_behaviorally_inert_on_repeatability_limit``,
    and cutting it now only to re-add it for the same future seam later
    would cost more than carrying it.
    """
    own_n = 1 + len(own.repeat_responses)
    sibling_n = 1 + len(sibling.repeat_responses)
    if (
        own_n < LINEARIZATION_MIN_PAIRED_OCCURRENCES
        or sibling_n < LINEARIZATION_MIN_PAIRED_OCCURRENCES
    ):
        return None
    live = compute_sigma_curve(own, valid_band_hz=valid_band_hz, grid_hz=grid_hz)
    if live is None:
        return None
    floor_db = _SIGMA_TOLERABLE_DB[tier]
    return np.maximum(floor_db, live)


# --------------------------------------------------------------------------- #
# seams + snapshot
# --------------------------------------------------------------------------- #

# Injected seams. The web host binds the production implementations
# (jasper.web.correction_crossover_v2); tests inject fakes.
PlayProgram = Callable[[str, ExcitationProgram], None]
# analyze(program, capture_result, priors, geometry) → ProgramAnalysis. The
# second argument is the relay CaptureResult (wav + phone-reported device +
# setup — the production binding resolves the mic calibration from it; fakes
# may pass raw bytes). ``geometry`` is the conductor's declared
# MeasurementGeometry so the parallax correction actually reaches
# analyze_program_capture — a seam that dropped it would silently analyze
# with zero spacing.
AnalyzeCapture = Callable[
    [ExcitationProgram, Any, MeasurementPriors, MeasurementGeometry],
    ProgramAnalysis,
]
PublishCheck = Callable[[GainPlan, Mapping[str, Any]], None]
PublishCandidate = Callable[[Any], None]
ApplyGate = Callable[[], bool]
# Reads whether the conductor's own auto-apply (triggered by the host after a
# trusted MEASURE accept, §owner ruling 2026-07-20) hit a TERMINAL failure —
# returns the reason code (e.g. REASON_APPLY_FAILED) or "" while still
# pending/never attempted. Distinct from ``apply_complete`` (success only) so
# ``authorize_begin`` can REFUSE the deferred VERIFY with an honest reason
# instead of holding forever toward a dishonest relay_timeout.
ApplyFailureGate = Callable[[], str]


@dataclass(frozen=True)
class V2FlowSeams:
    """The conductor's injected I/O boundary (all side effects)."""

    play: PlayProgram
    analyze: AnalyzeCapture
    publish_check: PublishCheck
    publish_candidate: PublishCandidate
    apply_complete: ApplyGate
    apply_failed: ApplyFailureGate


@dataclass(frozen=True)
class V2ConductorSnapshot:
    """Durable phase state, bound to the relay session (§5.6).

    Persisted under the session's commissioning run; :meth:`CrossoverV2Conductor.hydrate`
    keeps the accepted phases only when the current session matches — a new
    session invalidates CHECK/MEASURE evidence (mic position is unverifiable
    across sessions).
    """

    session_id: str
    accepted_phases: tuple[str, ...] = ()
    applied: bool = False
    gain_plan_db: Mapping[str, float] | None = None
    candidate_fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "accepted_phases": list(self.accepted_phases),
            "applied": self.applied,
            "gain_plan_db": dict(self.gain_plan_db) if self.gain_plan_db else None,
            "candidate_fingerprint": self.candidate_fingerprint,
        }


@dataclass(frozen=True)
class PhaseVerdict:
    """A consume verdict: the relay dict + the internal reason (if any)."""

    accepted: bool
    code: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_relay_dict(self) -> dict[str, Any]:
        """The mapping ``consume_capture`` returns to ``run_capture_plan``.

        Always carries ``accepted``; a rejection adds the reason code + template
        + copy so the phone renders the right §5.10 screen. Every non-``accepted``
        field is relayed verbatim in the ``capture_result`` host event.
        """
        out: dict[str, Any] = {"accepted": self.accepted}
        if self.code is not None:
            spec = REASON_REGISTRY[self.code]
            out.update(
                code=self.code,
                template=spec.template,
                reason=spec.message or spec.banner,
                banner=spec.banner,
                auto_retry=self.code in TRANSIENT_AUTO_RETRY_CODES,
            )
        out.update(self.payload)
        return out


# --------------------------------------------------------------------------- #
# the conductor
# --------------------------------------------------------------------------- #


class CrossoverV2Conductor:
    """The v2 phase state machine driving one relay capture session.

    Construct with the session identity, the declared drivers, the crossover Fc,
    the safety caps + session volume, and the injected :class:`V2FlowSeams`.
    Hand :meth:`authorize_begin`, :meth:`on_armed`, and :meth:`consume_capture`
    to :func:`jasper.capture_relay.session.run_capture_plan`; call
    :meth:`note_apply_complete` once the host's own auto-apply lands (the
    deferred VERIFY then arms) — an optional synchronous shortcut for a caller
    that already holds this conductor; the seam-based ``apply_complete``/
    ``apply_failed`` checks in :meth:`authorize_begin` are the durable path and
    work even without this call. :meth:`snapshot` / :meth:`hydrate` carry phase
    persistence.
    """

    def __init__(
        self,
        *,
        session_id: str,
        source_preset: Any,
        roles_bands: Sequence[RoleBand],
        fc_hz: float,
        driver_caps_dbfs: Mapping[str, float],
        session_volume_db: float,
        seams: V2FlowSeams,
        driver_spacing_m: float = 0.0,
        accepted_phases: Sequence[str] = (),
        applied: bool = False,
        gain_plan_db: Mapping[str, float] | None = None,
        index_phase_map: Mapping[int, str] | None = None,
        measure_predicted_sum: Any = None,
        measure_gate_window_ms: float | None = None,
        verify_pilot_transfer_baseline: Mapping[str, float] | None = None,
        driver_class_by_role: Mapping[str, str] | None = None,
    ) -> None:
        roles = tuple(roles_bands)
        if len(roles) != 2:
            raise CrossoverV2FlowError("the v2 conductor is a 2-way flow")
        self.session_id = str(session_id)
        self._preset = source_preset
        self._roles = roles
        self._woofer, self._tweeter = roles[0], roles[1]
        self._fc_hz = float(fc_hz)
        self._caps = dict(driver_caps_dbfs)
        self._session_volume_db = float(session_volume_db)
        self._seams = seams
        # Layer-1a linearization (#1668 PR-C): per-role driver class, used by
        # class_prior_limit(). "unknown" (the conservative default) until
        # #1665 lands component-entry declarations — no production caller
        # populates this yet, matching linearization_envelope.compose_envelope's
        # own "unknown" default.
        self._driver_class_by_role = (
            dict(driver_class_by_role) if driver_class_by_role else {}
        )
        self._geometry = MeasurementGeometry(
            driver_spacing_m=float(driver_spacing_m),
            mic_distance_m=MEASUREMENT_DISTANCE_M,
        )
        self._accepted = set(accepted_phases)
        self._applied = bool(applied)
        self._gain_plan_db = dict(gain_plan_db) if gain_plan_db else None
        # Relay capture-plan index → phase. The standard 3-entry session uses
        # the default; a verify-only re-arm session (§5.2 "Re-verify") maps its
        # single entry {1: PHASE_VERIFY}.
        self._index_phase_map = (
            dict(index_phase_map) if index_phase_map is not None else dict(_INDEX_PHASE)
        )

        # Programs — CHECK is composable now; MEASURE waits on the gain solve,
        # VERIFY on Fc (composable now, played only after apply).
        self._check_program = self._compose_check_program()
        self._measure_program: ExcitationProgram | None = (
            self._compose_measure_program(self._gain_plan_db)
            if self._gain_plan_db is not None
            else None
        )
        self._verify_program = self._compose_verify_program()

        # Per-phase attempt bookkeeping + the last failure reason.
        self._phase_attempts: dict[str, int] = {}
        self._last_reason: dict[str, str] = {}
        self._armed_index: int | None = None
        # The most recent authorized (index, attempt) — the host reads it to
        # address the terminal ``capture_result`` host event at a play-seam
        # failure (§5.10 / W6.1), so the phone stops waiting instead of
        # recording into silence forever.
        self._armed_capture: tuple[int, int] | None = None
        # MEASURE→VERIFY handoff evidence. A verify-only re-arm session
        # rehydrates both from the persisted state (§5.2 re-verify).
        self._measure_predicted_sum: Any = measure_predicted_sum
        self._measure_gate_window_ms: float | None = measure_gate_window_ms
        self._candidate: Any = None
        self._verify_outcome: str | None = None  # pass | fail | inconclusive
        # The VERIFY tracking numbers behind the verify_fail screen's collapsed
        # expert disclosure (#1605). Set only once the tolerance comparison is
        # actually reached (the tracking numbers exist); the early-return
        # verdicts (locate/agc/gate/level-shift) leave it None so no half-empty
        # disclosure renders.
        self._verify_evidence: dict[str, Any] | None = None
        self._last_failure_code: str | None = None
        # G3 (measurement-honesty gate, 2026-07-22): the FIRST usable VERIFY
        # attempt's per-role pilot transfer becomes the reference every LATER
        # attempt is compared against — never re-baselined once set (see
        # ``_verify_verdict``). A verify-only re-arm session
        # (``prepare_v2_verify``) rehydrates this from the prior session's
        # persisted ``verify_priors``, exactly like ``measure_gate_window_ms``
        # above; a fresh CHECK→MEASURE walk (``prepare_v2_session``) never
        # threads it, so a genuinely new measurement starts with no VERIFY
        # history to compare against (acceptable — see the property below).
        # Known limitation: the persisted baseline never expires or
        # re-baselines across verify-only re-arm sessions, so a PERSISTENT
        # (not transient) post-first-verify setup shift re-fires
        # verify_level_shift on every "Try again" until the household
        # re-measures or undoes — matching ``verify_out_of_tolerance``'s
        # pre-existing perpetual-retry shape when the speaker itself is
        # genuinely out of tolerance; the household-facing copy is
        # deliberately unchanged for this.
        self._verify_pilot_baseline: dict[str, float] | None = (
            dict(verify_pilot_transfer_baseline)
            if verify_pilot_transfer_baseline
            else None
        )
        # Transient, recomputed on every VERIFY attempt (never carried
        # forward itself) — this attempt's step vs the baseline above, or
        # ``None`` when there is nothing to compare (no usable pilots this
        # attempt, no shared role with the baseline, or this very attempt is
        # the one that just established the baseline). ``_log_verify_diag``
        # reads it for the ``pilot_transfer_step_db`` diagnostic field.
        self._verify_pilot_transfer_step_db: float | None = None
        # Which (if any) measurement-honesty gate produced the LAST MEASURE
        # verdict — reset at the top of every ``_measure_verdict`` call so a
        # stale value from a PRIOR attempt can never leak into this attempt's
        # diagnostic. G1/G2 both reuse an existing reason code shared with a
        # pre-existing check (REASON_LOW_ALIGNMENT_CONFIDENCE /
        # REASON_DRIFT_BASELINES_DISAGREE respectively), so the reason code
        # alone cannot tell telemetry which check actually fired — this side
        # channel can. Read by ``_log_measure_diag``; never consulted by
        # ``_measure_verdict`` itself, so a bug here cannot change a verdict.
        self._last_measure_guard: str = ""
        # SF3 (2026-07-24 adversarial review): which linearization path this
        # attempt's candidate build took — set by ``_linearization_eligible``
        # (the ineligible branches) and ``_fit_linearization`` (fitted vs the
        # wild-trim sanity fallback) or ``_build_candidate`` (a raised fit
        # bug). Mirrors ``_last_measure_guard`` exactly: reset at the top of
        # every ``_measure_verdict`` call so a stale value from a PRIOR
        # attempt — or from a verdict that never reached ``_build_candidate``
        # — can never leak into this attempt's diagnostic. One of "",
        # "ineligible_mic_tier", "ineligible_repeats", "fitted",
        # "trim_rejected", or "fit_failed"; empty means "not evaluated this
        # attempt." Read by ``_log_measure_diag``'s ``linearization=`` field;
        # never consulted by ``_measure_verdict`` itself, so a bug here
        # cannot change a verdict.
        self._last_linearization_outcome: str = ""
        # VERIFY-prediction coherence fix (hardware-validation-caught, #1668
        # PR-D): stamped by ``_fit_linearization`` on the SAME "fitted"/
        # "trim_rejected" sub-outcomes as ``_last_linearization_outcome``
        # above — both emit the correction filters into the live graph (only
        # the trim differs between them), so both need the persisted VERIFY
        # prediction rebuilt from the LINEARIZED branches, never the raw
        # ones. ``None`` on every other path (ineligible, fit_failed, or a
        # verdict that never reached ``_build_candidate`` this attempt),
        # which ``_measure_verdict`` reads as "use ``analysis.predicted_sum``
        # (the raw branches) instead" — byte-identical to before this fix.
        # Reset at the top of every ``_measure_verdict`` call, mirroring
        # ``_last_linearization_outcome``'s own reset discipline: a stale
        # value from a PRIOR attempt must never leak into THIS attempt's
        # persisted VERIFY prior.
        self._last_linearized_predicted_sum: tuple[np.ndarray, np.ndarray] | None = None

    # --- program composition -------------------------------------------------

    def _compose_check_program(self) -> ExcitationProgram:
        # Cap-aware (W6.1): each driver's pilot base is clamped so the loudest
        # (hi) pilot's effective peak stays under that driver's cap folded
        # through the session volume — the same ``back_off_gain`` margin the
        # MEASURE composer uses. The tweeter (compression driver, deep cap)
        # rides a base ~40 dB below the woofer's; both pilots keep their fixed
        # ``DEFAULT_PILOT_LEVELS_DB`` offsets against that per-role base, so the
        # 10 dB behavioral-linearity delta is preserved while the absolute
        # level degrades honestly (recorded in the segment gains). Before this
        # the CHECK program used the shared reference base and admission
        # refused it on the JTS3 tweeter (program_channel_peak_over_cap).
        role_base = {
            rb.role: back_off_gain(
                BASE_STIMULUS_PEAK_DBFS,
                self._session_volume_db,
                self._caps.get(rb.role, 0.0),
            )
            for rb in self._roles
        }
        return build_check_program(
            self._roles,
            downstream_gain_db=self._session_volume_db,
            role_base_peak_dbfs=role_base,
            courtesy_prelude=COURTESY_PRELUDE_ENABLED,
        )

    def _pilot_gains(self, hi_gain_db: float) -> tuple[float, float]:
        return (hi_gain_db - PILOT_LEVEL_DELTA_DB, hi_gain_db)

    def _compose_measure_program(
        self, gain_plan_db: Mapping[str, float], *, extra_backoff_db: float = 0.0,
    ) -> ExcitationProgram:
        gains = {}
        for rb in self._roles:
            cap = self._caps.get(rb.role, 0.0)
            gains[rb.role] = back_off_gain(
                float(gain_plan_db[rb.role]) - extra_backoff_db,
                self._session_volume_db,
                cap,
            )
        return build_measure_program(
            gains, self._roles,
            downstream_gain_db=self._session_volume_db,
            leading_pilot_gains_db=self._pilot_gains(gains[self._woofer.role]),
            leading_pilot_role=self._woofer.role,
            courtesy_prelude=COURTESY_PRELUDE_ENABLED,
        )

    def _compose_verify_program(self, *, extra_backoff_db: float = 0.0) -> ExcitationProgram:
        # Cap-aware (W6.1): VERIFY plays a MONO summed sweep through the APPLIED
        # production graph with NO play-time admission gate (it does not ride
        # ``play_program``/``readmit`` — see ``bind_production_play``), so the
        # compose-time clamp is the ONLY level guard. A summed signal reaches
        # every driver, so it is clamped to the MOST RESTRICTIVE (min) cap: at
        # the worst case (no crossover attenuation) no driver is driven past its
        # own limit. Without this the summed sweep played at the shared
        # reference base (effective ~-32 dBFS) would over-drive a deep-cap
        # tweeter (e.g. the JTS3 B&C DE250 at -65 dBFS effective). The
        # ``_pilot_gains`` pair rides the same clamped level, so its 10 dB delta
        # is preserved. A genuinely-too-quiet clamp surfaces as the existing
        # snr_floor / agc_behavioral_fail verdicts, not a precheck (§5.10).
        binding_cap = min(self._caps.values()) if self._caps else 0.0
        gain = back_off_gain(
            BASE_STIMULUS_PEAK_DBFS - extra_backoff_db,
            self._session_volume_db,
            binding_cap,
        )
        return build_verify_program(
            self._fc_hz,
            gain_db=gain,
            downstream_gain_db=self._session_volume_db,
            leading_pilot_gains_db=self._pilot_gains(gain),
            courtesy_prelude=COURTESY_PRELUDE_ENABLED,
        )

    # --- priors per phase ----------------------------------------------------

    def _measure_priors(self) -> MeasurementPriors:
        return MeasurementPriors(
            crossover_fc_hz=self._fc_hz,
            alignment_delay_bounds_us=alignment_delay_search_bounds_us(self._preset),
        )

    def _verify_priors(self) -> MeasurementPriors:
        # Carry MEASURE's actual per-driver sweep bounds forward (§5.6 fix) so
        # VERIFY's tracking comparison trusts the SAME true driver-sweep
        # overlap `_build_candidate` used to build `predicted_sum` — never a
        # hardcoded frequency, always read off the composed MEASURE program.
        tweeter_sweep_lo_hz: float | None = None
        woofer_sweep_hi_hz: float | None = None
        if self._measure_program is not None:
            try:
                tweeter_sweep_lo_hz = self._measure_program.segment("sweep_t").f1_hz
                woofer_sweep_hi_hz = self._measure_program.segment("sweep_w").f2_hz
            except KeyError:
                pass
        return MeasurementPriors(
            crossover_fc_hz=self._fc_hz,
            predicted_sum=self._measure_predicted_sum,
            measure_tweeter_sweep_lo_hz=tweeter_sweep_lo_hz,
            measure_woofer_sweep_hi_hz=woofer_sweep_hi_hz,
        )

    # --- read surfaces -------------------------------------------------------

    @property
    def accepted_phases(self) -> frozenset[str]:
        return frozenset(self._accepted)

    def phase_status(self, phase: str) -> str:
        return "accepted" if phase in self._accepted else "pending"

    def pending_phases(self) -> tuple[str, ...]:
        return tuple(p for p in CAPTURE_PHASES if p not in self._accepted)

    @property
    def current_phase(self) -> str:
        for phase in CAPTURE_PHASES:
            if phase not in self._accepted:
                # MEASURE accepted but not yet applied ⇒ the conductor's own
                # auto-apply is in flight (or has failed) — no human control
                # page, just a brief machine-paced window before VERIFY arms.
                if phase == PHASE_VERIFY and PHASE_MEASURE in self._accepted and not self._applied:
                    return PHASE_APPLYING
                return phase
        return PHASE_DONE

    @property
    def candidate(self) -> Any:
        return self._candidate

    @property
    def verify_outcome(self) -> str | None:
        return self._verify_outcome

    @property
    def verify_evidence(self) -> dict[str, Any] | None:
        """The verify_fail expert-disclosure numbers (#1605), or None."""
        return dict(self._verify_evidence) if self._verify_evidence else None

    @property
    def applied(self) -> bool:
        return self._applied

    @property
    def measure_predicted_sum(self) -> Any:
        return self._measure_predicted_sum

    @property
    def measure_gate_window_ms(self) -> float | None:
        return self._measure_gate_window_ms

    @property
    def verify_pilot_transfer_baseline(self) -> Mapping[str, float] | None:
        """The frozen G3 reference (host persistence reads it, mirroring
        ``measure_gate_window_ms`` above — see ``__init__``'s comment)."""
        return (
            dict(self._verify_pilot_baseline)
            if self._verify_pilot_baseline is not None
            else None
        )

    @property
    def last_failure_code(self) -> str | None:
        """The most recent rejection's reason code (host persistence reads it)."""
        return self._last_failure_code

    @property
    def armed_capture(self) -> tuple[int, int] | None:
        """The last authorized ``(index, attempt)`` — the host addresses the
        terminal ``capture_result`` host event at a play-seam failure to it."""
        return self._armed_capture

    def _phase_of_index(self, index: int) -> str:
        phase = self._index_phase_map.get(index)
        if phase is None:
            raise CrossoverV2FlowError(f"no v2 phase for capture index {index}")
        return phase

    # --- lifecycle -----------------------------------------------------------

    def note_apply_complete(self) -> None:
        """The apply-complete host event — arms the soft-held VERIFY (§5.2)."""
        self._applied = True
        log_event(
            logger, "correction.crossover_v2_apply_complete",
            session_id=self.session_id,
        )

    def _apply_observed(self) -> bool:
        if self._applied:
            return True
        try:
            observed = bool(self._seams.apply_complete())
        except (OSError, RuntimeError, ValueError):
            observed = False
        if observed:
            self._applied = True
        return observed

    def snapshot(self) -> V2ConductorSnapshot:
        return V2ConductorSnapshot(
            session_id=self.session_id,
            accepted_phases=tuple(p for p in CAPTURE_PHASES if p in self._accepted),
            applied=self._applied,
            gain_plan_db=dict(self._gain_plan_db) if self._gain_plan_db else None,
            candidate_fingerprint=(
                getattr(self._candidate, "fingerprint", None)
                if self._candidate is not None else None
            ),
        )

    @classmethod
    def hydrate(
        cls,
        snapshot: V2ConductorSnapshot | None,
        *,
        session_id: str,
        **kwargs: Any,
    ) -> "CrossoverV2Conductor":
        """Rebuild a conductor, applying the §5.6 session-binding rule.

        Same session ⇒ resume, keeping the accepted phases + gain plan (skips
        accepted phases). A different or absent session ⇒ fresh start at CHECK
        (CHECK/MEASURE evidence invalidated — mic position is unverifiable
        across sessions).
        """
        if snapshot is not None and snapshot.session_id == session_id:
            return cls(
                session_id=session_id,
                accepted_phases=snapshot.accepted_phases,
                applied=snapshot.applied,
                gain_plan_db=snapshot.gain_plan_db,
                **kwargs,
            )
        if snapshot is not None:
            log_event(
                logger, "correction.crossover_v2_session_rebound",
                level=logging.INFO,
                prior_session=snapshot.session_id,
                session_id=session_id,
            )
        return cls(session_id=session_id, **kwargs)

    # --- relay callbacks -----------------------------------------------------

    def authorize_begin(self, index: int, attempt: int, entry: Any = None) -> None:
        """Admit (or defer / refuse) one phone ``begin_capture`` (§5.7).

        VERIFY is soft-held (:class:`CaptureBeginDeferred`) until the
        conductor's own auto-apply is observed (never a human tap, since the
        2026-07-20 owner ruling); a phase whose retry budget is spent is
        refused (:class:`CaptureBeginRefused`, which ends the session so the
        envelope's terminal screen shows). If the auto-apply hit a TERMINAL
        failure (``seams.apply_failed()`` names a reason), the hold is refused
        outright rather than held toward a dishonest relay_timeout — the
        household sees the real reason, not a manufactured "link timed out."
        Every other begin is admitted.
        """
        phase = self._phase_of_index(index)
        if phase == PHASE_VERIFY and not self._apply_observed():
            failure_code = ""
            try:
                failure_code = str(self._seams.apply_failed() or "")
            except (OSError, RuntimeError, ValueError):
                failure_code = ""
            if failure_code:
                self._last_failure_code = failure_code
                spec = REASON_REGISTRY.get(failure_code)
                message = spec.message or spec.banner if spec else failure_code
                raise CaptureBeginRefused(failure_code, message)
            raise CaptureBeginDeferred(
                "awaiting_apply",
                "Applying the measured crossover to your speaker…",
            )
        # Budget: CUMULATIVE per phase by design — the phase's total attempt
        # count is compared against the LAST failure's retry budget, so
        # alternating reason codes cannot restart the meter (a capture that
        # fails `clipped` then `locate_failed` then `clipped`... would retry
        # forever under a literal per-code reading of the §5.10 budget
        # column). This is deliberately stricter than §5.10 read per-code;
        # the plan's `max_attempts` (8) bounds the whole session regardless.
        # First attempt of any phase is always admitted.
        count = self._phase_attempts.get(phase, 0) + 1
        last = self._last_reason.get(phase)
        if last is not None and count > REASON_REGISTRY[last].retry_budget + 1:
            spec = REASON_REGISTRY[last]
            raise CaptureBeginRefused(spec.code, spec.message or spec.banner)
        self._phase_attempts[phase] = count
        self._armed_index = index
        self._armed_capture = (index, attempt)
        log_event(
            logger, "correction.crossover_v2_authorized",
            session_id=self.session_id, phase=phase, index=index, attempt=attempt,
        )

    def on_armed(self, state: Any = None) -> None:
        """Play the armed phase's excitation program (the host stimulus)."""
        index = self._armed_index
        if index is None:
            raise CrossoverV2FlowError("on_armed with no authorized capture")
        phase = self._phase_of_index(index)
        program = self._program_for_phase(phase)
        log_event(
            logger, "correction.crossover_v2_play",
            session_id=self.session_id, phase=phase, program_id=program.program_id,
        )
        self._seams.play(phase, program)

    def _program_for_phase(self, phase: str) -> ExcitationProgram:
        if phase == PHASE_CHECK:
            return self._check_program
        if phase == PHASE_MEASURE:
            if self._measure_program is None:
                raise CrossoverV2FlowError(
                    "MEASURE armed before the CHECK gain solve produced a program"
                )
            return self._measure_program
        if phase == PHASE_VERIFY:
            return self._verify_program
        raise CrossoverV2FlowError(f"no program for phase {phase!r}")

    def consume_capture(
        self, index: int, attempt: int, result: Any, entry: Any = None,
    ) -> dict[str, Any]:
        """Analyze one uploaded capture and advance (or reject) the phase."""
        phase = self._phase_of_index(index)
        program = self._program_for_phase(phase)
        priors = (
            self._measure_priors() if phase == PHASE_MEASURE
            else self._verify_priors() if phase == PHASE_VERIFY
            else MeasurementPriors()
        )
        # The whole CaptureResult crosses the seam (not just wav bytes): the
        # production analyze binding resolves the mic calibration from the
        # phone-reported setup/device, and the conductor's declared geometry
        # rides along so the parallax correction reaches the analysis.
        analysis = self._seams.analyze(program, result, priors, self._geometry)
        if phase == PHASE_CHECK:
            verdict = self._consume_check(analysis)
        elif phase == PHASE_MEASURE:
            verdict = self._consume_measure(analysis)
        else:
            verdict = self._consume_verify(analysis)
        if verdict.accepted:
            self._accepted.add(phase)
            self._last_reason.pop(phase, None)
            self._last_failure_code = None
        elif verdict.code is not None:
            self._last_reason[phase] = verdict.code
            self._last_failure_code = verdict.code
        log_event(
            logger, "correction.crossover_v2_result",
            session_id=self.session_id, phase=phase,
            accepted=verdict.accepted, code=verdict.code or "",
        )
        return verdict.to_relay_dict()

    # --- per-phase verdicts --------------------------------------------------
    #
    # Each ``_consume_<phase>`` is a thin wrapper: compute the verdict via the
    # UNCHANGED ``_<phase>_verdict`` logic, log that capture's full numeric
    # diagnostics (Part 1 — on the accepted path AND every rejection) through
    # ``_safe_log_diag`` — never the raw ``_log_*_diag`` call directly, so a
    # bug in the logging path can never crash or flip the verdict already
    # decided above it — then return the verdict. Splitting it this way means
    # the diagnostic log call is the ONLY new control flow here — none of the
    # accept/reject branching below moved or changed.

    def _consume_check(self, analysis: ProgramAnalysis) -> PhaseVerdict:
        verdict = self._check_verdict(analysis)
        self._safe_log_diag(self._log_check_diag, analysis, verdict)
        return verdict

    def _check_verdict(self, analysis: ProgramAnalysis) -> PhaseVerdict:
        if not _stimulus_locate_ok(analysis):
            return PhaseVerdict(False, REASON_LOCATE_FAILED)
        if analysis.channel_map_ok is False:
            return PhaseVerdict(False, REASON_CHANNEL_MAP_MISMATCH)
        if analysis.pilot_snr_ok is False:
            # Band-relative ambient-compensated linearity fix (2026-07-20):
            # the quiet pilot's own in-band SNR was too low to trust the
            # ambient-subtracted delta either way — ``analysis.linearity_ok``
            # is already forced True in this case (see
            # ``program_analysis._pilot_observations``'s docstring), so this
            # branch is the ONLY path that can fail on it. Route to the
            # honest room/positioning reason, never AGC — the phone's mic
            # didn't misbehave, there just wasn't enough signal above the
            # room to measure.
            return PhaseVerdict(False, REASON_SNR_FLOOR)
        if analysis.linearity_ok is False:
            # W6.12: don't blame the phone's mic when the room was the actual
            # cause. The CHECK gain solve ALREADY computes an SNR-floor
            # verdict against THIS capture's own ambient bands (``_analyze_check``
            # runs ``_solve_gain_plan`` unconditionally, before this branch),
            # independent of whether linearity itself passed — reuse that
            # existing evidence rather than re-deriving a second ambient
            # judgment. Only CHECK gets this distinction: MEASURE/VERIFY's
            # leading pilot pair has no ambient window of its own (see
            # ``_pilot_verdicts``'s docstring), so there is no comparably
            # clean signal to judge "was the room loud" there yet.
            if analysis.gain_plan is not None and not analysis.gain_plan.snr_floor_ok:
                return PhaseVerdict(False, REASON_NOISY_ROOM_LINEARITY)
            return PhaseVerdict(False, REASON_AGC_BEHAVIORAL_FAIL)
        gain_plan = analysis.gain_plan
        if gain_plan is None or not gain_plan.snr_floor_ok:
            return PhaseVerdict(False, REASON_SNR_FLOOR)
        # Accept: keep the solved gains + ambient, compose the MEASURE program,
        # publish CHECK evidence.
        self._gain_plan_db = dict(gain_plan.gain_db)
        self._measure_program = self._compose_measure_program(self._gain_plan_db)
        self._seams.publish_check(gain_plan, analysis.ambient_report or {})
        return PhaseVerdict(True, payload={"measurement_phase": PHASE_CHECK})

    def _consume_measure(self, analysis: ProgramAnalysis) -> PhaseVerdict:
        verdict = self._measure_verdict(analysis)
        self._safe_log_diag(self._log_measure_diag, analysis, verdict)
        return verdict

    def _measure_verdict(self, analysis: ProgramAnalysis) -> PhaseVerdict:
        # Reset every call — a stale value from a PRIOR attempt must never
        # leak into THIS attempt's diagnostic (see __init__'s comment).
        self._last_measure_guard = ""
        self._last_linearization_outcome = ""
        self._last_linearized_predicted_sum = None
        if not _stimulus_locate_ok(analysis):
            return PhaseVerdict(False, REASON_LOCATE_FAILED)
        if analysis.glitch_detected:
            # Repeat-level disagreement reuses this same code (§5.2) — the
            # analysis already folded it into glitch_detected.
            self._rearm_measure_after_transient()
            return PhaseVerdict(False, REASON_DRIFT_BASELINES_DISAGREE)
        # Measurement-honesty gate G2 (2026-07-22 — the xrun detector): a
        # uniform whole-capture schedule shift the repeat-pair drift check
        # above is structurally blind to (see SWEEP_SCHEDULE_RESIDUAL_CEILING_MS
        # for the evidence). Routed identically to the glitch branch above —
        # same silent auto-retry, same reused reason code (§5.2's "never a
        # new user-facing code for a capture-glitch class" convention) — the
        # ``guard`` diag field (below) is what tells telemetry the two apart.
        # ``_program_for_phase`` (not the bare ``self._measure_program``,
        # which mypy types ``ExcitationProgram | None``) is the ALREADY
        # type-narrowed accessor — it raises if MEASURE were somehow armed
        # before CHECK produced a program, which can't happen on this path
        # (we are actively processing a MEASURE analysis).
        if not _sweep_schedule_ok(
            analysis, self._program_for_phase(PHASE_MEASURE).sample_rate_hz
        ):
            self._last_measure_guard = "sweep_schedule"
            self._rearm_measure_after_transient()
            return PhaseVerdict(False, REASON_DRIFT_BASELINES_DISAGREE)
        if _any_sweep_clipped(analysis):
            self._rearm_measure_after_transient(extra_backoff_db=CLIP_RETRY_BACKOFF_DB)
            return PhaseVerdict(False, REASON_CLIPPED)
        if analysis.linearity_ok is False:
            return PhaseVerdict(False, REASON_AGC_BEHAVIORAL_FAIL)
        if analysis.alignment is not None and analysis.alignment.status != ALIGNMENT_OK:
            return PhaseVerdict(False, REASON_DELAY_EXCEEDS_SEARCH_WINDOW)
        # Trust gate (owner ruling, 2026-07-20): this is GCC's capture/seed
        # confidence, not confidence in T2's refined delay (the alignment and
        # candidate retain both facts separately). Below the floor the
        # candidate is never built or published — a household has no basis to
        # judge a confidence number, so this is guidance ("move the mic"), not
        # a question ("apply anyway?"). Skipped entirely when there is no
        # alignment estimate at all (a trims-only candidate) — same condition
        # the former review-screen nudge used.
        if (
            analysis.alignment is not None
            and analysis.alignment.confidence < ALIGNMENT_CONFIDENCE_TRUST_FLOOR
        ):
            return PhaseVerdict(False, REASON_LOW_ALIGNMENT_CONFIDENCE)
        # Physical-plausibility backstop (Fix 3): a confidently-WRONG delay
        # (high GCC correlation confidence at the wrong lag) clears the trust
        # gate above but is still physically implausible against the
        # preset's declared search bound — reuses the SAME re-measure
        # guidance rather than a new reason code, since the household action
        # is identical ("move the mic, measure again").
        if (
            analysis.alignment is not None
            and analysis.alignment.status == ALIGNMENT_OK
            and not alignment_delay_plausible(analysis.alignment.delay_us, self._preset)
        ):
            return PhaseVerdict(False, REASON_LOW_ALIGNMENT_CONFIDENCE)
        # Measurement-honesty gate G1 (2026-07-22): a candidate whose OWN
        # predicted ripple is this bad is not a trustworthy basis for
        # auto-apply, regardless of what alignment confidence or the Fix 3
        # plausibility check above reported — see
        # MEASURE_PREDICTED_RIPPLE_CEILING_DB for the evidence. Reuses the
        # SAME re-measure guidance as the two checks above (identical
        # household action); the ``guard`` diag field disambiguates which of
        # the three actually fired. Skipped when there is no candidate or no
        # alignment estimate (a trims-only path) — mirrors the confidence
        # gate's own skip condition above.
        if (
            analysis.candidate is not None
            and analysis.alignment is not None
            and analysis.candidate.predicted_ripple_db > MEASURE_PREDICTED_RIPPLE_CEILING_DB
        ):
            self._last_measure_guard = "ripple_ceiling"
            return PhaseVerdict(False, REASON_LOW_ALIGNMENT_CONFIDENCE)
        candidate = self._build_candidate(analysis)
        self._candidate = candidate
        # VERIFY-prediction coherence fix (hardware-validation-caught, #1668
        # PR-D): when this attempt fitted Layer-1a linearization (fitted OR
        # trim_rejected — both emit the correction filters, see
        # ``_fit_linearization``'s tail), the persisted prediction VERIFY
        # compares against must be the LINEARIZED model, the exact thing the
        # emitted graph now carries — never the raw-branch one. The
        # ineligible/fit_failed path is untouched: ``_last_linearized_
        # predicted_sum`` stays ``None`` there, so this stays byte-identical
        # to ``analysis.predicted_sum``, exactly as before this fix.
        self._measure_predicted_sum = (
            self._last_linearized_predicted_sum
            if self._last_linearized_predicted_sum is not None
            else analysis.predicted_sum
        )
        self._measure_gate_window_ms = self._measure_gate(analysis)
        self._seams.publish_candidate(candidate)
        return PhaseVerdict(
            True,
            payload={
                "measurement_phase": PHASE_MEASURE,
                "candidate_fingerprint": candidate.fingerprint,
                # Tells the host to trigger auto-apply immediately (§owner
                # ruling) — every candidate that reaches this point already
                # passed the trust gate above, so this is unconditionally True
                # here, not a second decision.
                "auto_apply": True,
            },
        )

    def _consume_verify(self, analysis: ProgramAnalysis) -> PhaseVerdict:
        verdict = self._verify_verdict(analysis)
        self._safe_log_diag(self._log_verify_diag, analysis, verdict)
        return verdict

    def _verify_verdict(self, analysis: ProgramAnalysis) -> PhaseVerdict:
        # Reset every call — a stale value from a PRIOR attempt must never
        # leak into THIS attempt's diagnostic (mirrors ``_last_measure_guard``'s
        # method-top reset in ``_measure_verdict``, see its own comment).
        # Every early return below (locate_failed, agc_behavioral_fail,
        # gate-comparability) runs BEFORE the G3 block gets a chance to
        # recompute this, so it must not still hold a REAL step number from
        # an earlier attempt that happened to reach that block —
        # ``_log_verify_diag`` runs unconditionally after this method
        # returns and would otherwise misreport it as fresh.
        self._verify_pilot_transfer_step_db = None
        # Same reset discipline: only a verdict that reaches the tracking
        # comparison below carries expert-disclosure evidence (#1605); the
        # early returns must not surface a prior attempt's numbers.
        self._verify_evidence = None
        if not _stimulus_locate_ok(analysis):
            return PhaseVerdict(False, REASON_LOCATE_FAILED)
        if analysis.linearity_ok is False:
            return PhaseVerdict(False, REASON_AGC_BEHAVIORAL_FAIL)
        # Gate-comparability rule (§5.2): a shorter VERIFY gate manufactures
        # overlay differences that aren't driver alignment ⇒ inconclusive.
        verify_gate = _gate_window_ms(analysis.summed_response)
        if (
            self._measure_gate_window_ms is not None
            and verify_gate is not None
            and verify_gate + 1e-6 < self._measure_gate_window_ms
        ):
            self._verify_outcome = "inconclusive"
            return PhaseVerdict(False, REASON_VERIFY_INCONCLUSIVE)
        # Measurement-honesty gate G3 (2026-07-22): the tracking-max
        # comparison below is exactly the thing a shifted recording chain
        # invalidates, so check the chain's OWN consistency first — this
        # gate is level-independent (unlike gate-comparability above, which
        # must stay first regardless). VERIFY replays the identical program
        # through the identical applied graph on every attempt, so its own
        # leading pilot pair's transfer (captured level minus programmed
        # gain) should not move between attempts either — see
        # VERIFY_PILOT_TRANSFER_STEP_CEILING_DB for the evidence. The FIRST
        # usable attempt of this conductor's own lifetime (never pilots
        # absent, never a legacy program missing ``programmed_hi_gain_db``)
        # only records the reference; it never rejects on this attempt.
        transfer = _pilot_transfer_by_role(analysis)
        if transfer:
            if self._verify_pilot_baseline is None:
                self._verify_pilot_baseline = dict(transfer)
            else:
                shared = [r for r in transfer if r in self._verify_pilot_baseline]
                if shared:
                    self._verify_pilot_transfer_step_db = max(
                        abs(transfer[r] - self._verify_pilot_baseline[r])
                        for r in shared
                    )
        if (
            self._verify_pilot_transfer_step_db is not None
            and self._verify_pilot_transfer_step_db > VERIFY_PILOT_TRANSFER_STEP_CEILING_DB
        ):
            self._verify_outcome = "inconclusive"
            return PhaseVerdict(False, REASON_VERIFY_LEVEL_SHIFT)
        tracking = analysis.verify_tracking or {}
        self._verify_evidence = _verify_evidence_from_tracking(tracking)
        # Flatness-verify (#1668 PR-D): a SIBLING report, relayed alongside
        # integration-verify's own tracking on BOTH branches below. Never
        # consulted by accepted/code — see FLATNESS_VERIFY_HI_HZ's own
        # comment for why the two claims stay separate (design doc
        # "Verification splits into two named claims").
        flatness_payload = (
            dict(analysis.flatness_tracking) if analysis.flatness_tracking else None
        )
        # Notch-aware, validity-floor-clamped comparator (W6.7 ruling 1 + W6.9
        # forensics): gate on the NOTCH-EXCLUDED max, not the raw full-band
        # max — and both are now computed over `tracking["tracking_band_hz"]`,
        # this capture's own gate-derived validity floor clamped up from the
        # nominal band (`program_analysis._analyze_verify`), not the nominal
        # [Fc/2, 2·Fc] band alone. Inside a predicted interference notch, or
        # below measurement validity, depth/level agreement is hypersensitive
        # to sub-dB/sub-degree branch differences (or outright unmeasurable)
        # and is not a meaningful tracking signal — the run-7 hardware failure
        # (27.83 dB raw max, against a predicted sum whose OWN ripple was
        # ~30 dB) was entirely that; the run-7/8 sequel traced the SAME class
        # of false divergence to a fixed-window prediction baking a room
        # reflection into a sub-floor region the notch rule alone didn't
        # always catch. ``max_db``/``rms_db`` (still clamped, just not
        # notch-excluded) and the pre-clamp ``*_full_band`` numbers still
        # travel in the persisted evidence as diagnostic fields only.
        max_db = tracking.get("max_db_notch_excluded")
        if not isinstance(max_db, (int, float)) or max_db > VERIFY_TOLERANCE_DB:
            self._verify_outcome = "fail"
            return PhaseVerdict(
                False, REASON_VERIFY_OUT_OF_TOLERANCE,
                payload={
                    "tracking": dict(tracking),
                    "flatness_tracking": flatness_payload,
                },
            )
        self._verify_outcome = "pass"
        return PhaseVerdict(
            True, payload={
                "measurement_phase": PHASE_VERIFY,
                "tracking": dict(tracking),
                "flatness_tracking": flatness_payload,
            }
        )

    # --- diagnostic logging (Part 1) ------------------------------------------
    #
    # One ``log_event`` per consumed capture, on the accepted path AND every
    # rejection — pure observability, read-only against ``analysis``/the
    # conductor's own state. None of these calls choose a verdict or a retry;
    # they run AFTER the verdict already exists.

    def _safe_log_diag(
        self,
        log_fn: Callable[[ProgramAnalysis, PhaseVerdict], None],
        analysis: ProgramAnalysis,
        verdict: PhaseVerdict,
    ) -> None:
        """Best-effort wrapper around one ``_log_*_diag`` call.

        Symmetric with the capture-retention path's own best-effort
        guarantee (Part 2): a bug in diagnostic-field extraction (a malformed
        ``analysis``, an unexpected ``None``) must never crash the capture or
        change the verdict already decided by ``_<phase>_verdict`` above —
        it degrades to a WARN instead. The caught set matches the realistic
        failure modes of these read-only field-extraction calls (attribute/
        key/index access and numeric conversion on ``analysis``'s own
        fields) — never a bare ``except Exception``.
        """
        try:
            log_fn(analysis, verdict)
        except (AttributeError, TypeError, ValueError, KeyError, IndexError):
            log_event(
                logger, "correction.crossover_v2_diag_log_failed",
                level=logging.WARNING, session_id=self.session_id,
                phase=analysis.phase, exc_info=True,
            )

    def _log_check_diag(self, analysis: ProgramAnalysis, verdict: PhaseVerdict) -> None:
        woofer = _pilot_diag_fields(_pilot_by_role(analysis, self._woofer.role))
        tweeter = _pilot_diag_fields(_pilot_by_role(analysis, self._tweeter.role))
        log_event(
            logger, "correction.crossover_v2_check_diag",
            session_id=self.session_id, accepted=verdict.accepted, code=verdict.code or "",
            pilot_snr_ok=analysis.pilot_snr_ok,
            woofer_snr_db=woofer["snr_db"],
            woofer_captured_delta_db=woofer["captured_delta_db"],
            woofer_programmed_delta_db=woofer["programmed_delta_db"],
            woofer_channel_map_target_rise_db=woofer["channel_map_target_rise_db"],
            woofer_channel_map_cross_rise_db=woofer["channel_map_cross_rise_db"],
            tweeter_snr_db=tweeter["snr_db"],
            tweeter_captured_delta_db=tweeter["captured_delta_db"],
            tweeter_programmed_delta_db=tweeter["programmed_delta_db"],
            tweeter_channel_map_target_rise_db=tweeter["channel_map_target_rise_db"],
            tweeter_channel_map_cross_rise_db=tweeter["channel_map_cross_rise_db"],
        )

    def _log_measure_diag(self, analysis: ProgramAnalysis, verdict: PhaseVerdict) -> None:
        drift = analysis.drift
        align = analysis.alignment
        cand = analysis.candidate
        delay_us, delay_role, polarity = alignment_to_candidate_fields(
            analysis, woofer_role=self._woofer.role, tweeter_role=self._tweeter.role,
        )
        woofer_snr_db, woofer_snr_verdict = _driver_snr_fields(
            _driver_response_by_role(analysis, self._woofer.role)
        )
        tweeter_snr_db, tweeter_snr_verdict = _driver_snr_fields(
            _driver_response_by_role(analysis, self._tweeter.role)
        )
        sweep_residual_ms_worst, sweep_locate_confidence_min = _sweep_schedule_diag_fields(
            analysis, self._program_for_phase(PHASE_MEASURE).sample_rate_hz
        )
        # First-vs-last per-role epsilon (sweep-composition PR-A, #1668) —
        # diagnostic only, never gated (DriftEstimate.per_role_epsilon_ppm's
        # own docstring). None-safe for a legacy construction site that
        # predates the field (empty mapping) or a role absent from it (<2
        # located occurrences that role).
        woofer_repeat_epsilon_ppm = (
            drift.per_role_epsilon_ppm.get(self._woofer.role) if drift else None
        )
        tweeter_repeat_epsilon_ppm = (
            drift.per_role_epsilon_ppm.get(self._tweeter.role) if drift else None
        )
        log_event(
            logger, "correction.crossover_v2_measure_diag",
            session_id=self.session_id, accepted=verdict.accepted, code=verdict.code or "",
            alignment_confidence=round(float(align.confidence), 4) if align else None,
            alignment_confidence_source=(align.confidence_source if align else None),
            alignment_seed_delay_us=(
                round(float(align.seed_delay_us), 3)
                if align and align.seed_delay_us is not None else None
            ),
            alignment_refinement_delta_us=(
                round(float(align.delay_us - align.seed_delay_us), 3)
                if align and align.seed_delay_us is not None else None
            ),
            gate_window_ms=self._measure_gate(analysis),
            validity_floor_hz=_measure_validity_floor_hz(analysis),
            epsilon_ppm=round(float(drift.epsilon_ppm), 3) if drift else None,
            max_residual_samples=round(float(drift.max_residual_samples), 3) if drift else None,
            repeat_level_delta_db=(
                round(float(drift.repeat_level_delta_db), 3) if drift else None
            ),
            woofer_repeat_epsilon_ppm=(
                round(float(woofer_repeat_epsilon_ppm), 3)
                if woofer_repeat_epsilon_ppm is not None else None
            ),
            tweeter_repeat_epsilon_ppm=(
                round(float(tweeter_repeat_epsilon_ppm), 3)
                if tweeter_repeat_epsilon_ppm is not None else None
            ),
            delay_us=round(delay_us, 3) if delay_us is not None else None,
            delay_role=delay_role,
            polarity=polarity,
            predicted_ripple_db=(
                round(float(cand.predicted_ripple_db), 4) if cand else None
            ),
            # #1667: how far the RAW candidate's (ripple-optimal-where-
            # trusted) tweeter trim moved from solve_branch_trims's
            # band-average seed — this always reports the RAW candidate's
            # own recovery, even on a linearization-eligible attempt (the
            # linearized path's own recovery travels separately in the
            # evidence JSON). The sanity-guard fallback path reads as
            # exactly 0.0 (raw == seed); ``None`` only when this candidate
            # predates trim_band_average_db.
            trim_ripple_gain_db=(
                round(
                    float(
                        cand.trim_db[self._tweeter.role]
                        - cand.trim_band_average_db[self._tweeter.role]
                    ),
                    4,
                )
                if cand and cand.trim_band_average_db is not None else None
            ),
            alignment_seed_ripple_db=(
                round(float(cand.alignment_seed_ripple_db), 4)
                if cand and cand.alignment_seed_ripple_db is not None else None
            ),
            flatness_improvement_db=(
                round(float(cand.flatness_improvement_db), 4)
                if cand and cand.flatness_improvement_db is not None else None
            ),
            anchor_delay_us=(
                round(float(cand.anchor_delay_us), 3)
                if cand and cand.anchor_delay_us is not None else None
            ),
            snap_delta_us=(
                round(float(cand.snap_delta_us), 3)
                if cand and cand.snap_delta_us is not None else None
            ),
            snap_found=(bool(cand.snap_found) if cand else None),
            woofer_snr_db=woofer_snr_db,
            woofer_snr_verdict=woofer_snr_verdict,
            tweeter_snr_db=tweeter_snr_db,
            tweeter_snr_verdict=tweeter_snr_verdict,
            sweep_residual_ms_worst=(
                round(sweep_residual_ms_worst, 3)
                if sweep_residual_ms_worst is not None else None
            ),
            sweep_locate_confidence_min=(
                round(sweep_locate_confidence_min, 4)
                if sweep_locate_confidence_min is not None else None
            ),
            # Which (if any) measurement-honesty gate fired this verdict —
            # disambiguates a G1/G2 fire from the pre-existing check that
            # shares its reused reason code (see __init__'s comment on
            # ``_last_measure_guard``).
            guard=self._last_measure_guard,
            # SF3 (adversarial review): which linearization path this
            # attempt's candidate build took — "" when the verdict was
            # rejected before ``_build_candidate`` ever ran (see __init__'s
            # comment on ``_last_linearization_outcome``).
            linearization=self._last_linearization_outcome,
        )

    def _log_verify_diag(self, analysis: ProgramAnalysis, verdict: PhaseVerdict) -> None:
        tracking = analysis.verify_tracking or {}
        band = tracking.get("tracking_band_hz")
        tracking_band_lo_hz: float | None = None
        tracking_band_hi_hz: float | None = None
        if isinstance(band, (list, tuple)) and len(band) == 2:
            tracking_band_lo_hz, tracking_band_hi_hz = band[0], band[1]
        validity_floor_hz = (
            analysis.summed_response.validity_floor_hz
            if analysis.summed_response is not None else None
        )
        # Flatness-verify (#1668 PR-D): a SIBLING claim, logged with its own
        # flatness_-prefixed fields alongside integration-verify's above —
        # never folded into the tracking_*/rms_db/max_db fields (see
        # FLATNESS_VERIFY_HI_HZ's own comment).
        flatness = analysis.flatness_tracking or {}
        flatness_band = flatness.get("band_hz")
        flatness_band_lo_hz: float | None = None
        flatness_band_hi_hz: float | None = None
        if isinstance(flatness_band, (list, tuple)) and len(flatness_band) == 2:
            flatness_band_lo_hz, flatness_band_hi_hz = flatness_band[0], flatness_band[1]
        # Measurement-honesty gate G3's own diagnostics: the current
        # attempt's raw pilot transfer (re-derived fresh, read-only — never
        # the mutated conductor state) and the step vs baseline
        # ``_verify_verdict`` already computed and stashed transiently.
        pilot_transfer_db = _pilot_transfer_by_role(analysis).get(VERIFY_PILOT_ROLE)
        log_event(
            logger, "correction.crossover_v2_verify_diag",
            session_id=self.session_id, accepted=verdict.accepted, code=verdict.code or "",
            max_db_notch_excluded=tracking.get("max_db_notch_excluded"),
            verify_tolerance_db=VERIFY_TOLERANCE_DB,
            verify_gate_window_ms=_gate_window_ms(analysis.summed_response),
            measure_gate_window_ms=self._measure_gate_window_ms,
            validity_floor_hz=validity_floor_hz,
            tracking_band_lo_hz=tracking_band_lo_hz,
            tracking_band_hi_hz=tracking_band_hi_hz,
            rms_db=tracking.get("rms_db"),
            flatness_rms_db=flatness.get("rms_db"),
            flatness_max_db=flatness.get("max_db"),
            flatness_tolerance_db=FLATNESS_VERIFY_TOLERANCE_DB,
            flatness_band_lo_hz=flatness_band_lo_hz,
            flatness_band_hi_hz=flatness_band_hi_hz,
            pilot_transfer_db=(
                round(pilot_transfer_db, 3) if pilot_transfer_db is not None else None
            ),
            pilot_transfer_step_db=(
                round(self._verify_pilot_transfer_step_db, 3)
                if self._verify_pilot_transfer_step_db is not None else None
            ),
            guard=(
                "pilot_level_shift" if verdict.code == REASON_VERIFY_LEVEL_SHIFT else ""
            ),
        )

    # --- helpers -------------------------------------------------------------

    def _rearm_measure_after_transient(self, *, extra_backoff_db: float = 0.0) -> None:
        """Recompose the MEASURE program for the automatic retry (§5.10 t1)."""
        if self._gain_plan_db is not None:
            self._measure_program = self._compose_measure_program(
                self._gain_plan_db, extra_backoff_db=extra_backoff_db
            )

    def _measure_gate(self, analysis: ProgramAnalysis) -> float | None:
        windows = [
            _gate_window_ms(resp) for resp in analysis.driver_responses
        ]
        finite = [w for w in windows if w is not None]
        return min(finite) if finite else None

    def _build_candidate(self, analysis: ProgramAnalysis) -> Any:
        from jasper.active_speaker.measured_crossover_candidate import (
            MeasuredCrossoverAlignment,
            MeasuredCrossoverCandidate,
        )

        cand = analysis.candidate
        if cand is None:
            raise CrossoverV2FlowError("MEASURE analysis produced no candidate")
        delay_us, delay_role, polarity = alignment_to_candidate_fields(
            analysis, woofer_role=self._woofer.role, tweeter_role=self._tweeter.role,
        )
        alignment = (
            MeasuredCrossoverAlignment(
                delay_us=delay_us, delay_role=delay_role, polarity=polarity,
            )
            if delay_role is not None
            else MeasuredCrossoverAlignment()
        )

        # Layer-1a driver linearization (#1668 PR-C). HARD GATE: reference-tier
        # mic AND both drivers paired N>=3 — anything else is byte-identical
        # to the pre-PR-C trims-only path (analysis.candidate.trim_db, empty
        # linearization dict). See _linearization_eligible/_fit_linearization.
        role_attenuations_db: Mapping[str, float] = dict(cand.trim_db)
        linearization: Mapping[str, Any] = {}
        if self._linearization_eligible(analysis):
            try:
                role_attenuations_db, linearization = self._fit_linearization(
                    analysis, cand
                )
            except (
                ArithmeticError, AttributeError, RuntimeError, TypeError, ValueError,
                KeyError, IndexError,
            ) as exc:
                # SF2 (adversarial review, 2026-07-24): the fit path is
                # strictly additive — an eligible speaker with a bug in the
                # (still-young) fit engine must degrade EXACTLY to the
                # ineligible path, never fail the whole MEASURE accept.
                # Mirrors _safe_log_diag's "never let enrichment logic break
                # the primary path" posture, one layer earlier (this guards
                # the candidate build itself, not just its diagnostic log
                # line). The caught set matches _safe_log_diag's own
                # (attribute/key/index/type/value access on structured
                # data), extended with ArithmeticError since this call site
                # does floating-point curve fitting (division, log,
                # exponentiation), not plain field extraction, and with
                # RuntimeError because linearization_fit.fit_driver_linearization
                # (N1, this same review) raises exactly that on its own
                # cut-only invariant violation — without it here, N1's safety
                # net would escape SF2's and crash this accept instead of
                # degrading to it.
                log_event(
                    logger, "correction.crossover_v2_linearization_fit_failed",
                    level=logging.WARNING, session_id=self.session_id,
                    reason=type(exc).__name__, exc_info=True,
                )
                role_attenuations_db = dict(cand.trim_db)
                linearization = {}
                self._last_linearization_outcome = "fit_failed"

        return MeasuredCrossoverCandidate(
            program_id=analysis.program_id,
            analysis=_analysis_json(analysis),
            source_preset=self._preset,
            role_attenuations_db=role_attenuations_db,
            alignment=alignment,
            linearization=linearization,
        )

    def _linearization_eligible(self, analysis: ProgramAnalysis) -> bool:
        """HARD GATE for the Layer-1a fit path: reference-tier mic AND both
        drivers paired N>=3 in-capture occurrences. Anything else falls back
        to the plain trims-only candidate, byte-identical to before this PR.

        Side effect: stamps ``self._last_linearization_outcome`` with WHY on
        every ineligible return (SF3) — mirrors ``_last_measure_guard``'s own
        set-during-the-walk convention; read by ``_log_measure_diag``.
        """
        if analysis.mic_tier != "reference":
            self._last_linearization_outcome = "ineligible_mic_tier"
            return False
        woofer_resp = _driver_response_by_role(analysis, self._woofer.role)
        tweeter_resp = _driver_response_by_role(analysis, self._tweeter.role)
        if woofer_resp is None or tweeter_resp is None:
            self._last_linearization_outcome = "ineligible_repeats"
            return False
        woofer_n = 1 + len(woofer_resp.repeat_responses)
        tweeter_n = 1 + len(tweeter_resp.repeat_responses)
        if (
            woofer_n >= LINEARIZATION_MIN_PAIRED_OCCURRENCES
            and tweeter_n >= LINEARIZATION_MIN_PAIRED_OCCURRENCES
        ):
            return True
        self._last_linearization_outcome = "ineligible_repeats"
        return False

    def _fit_linearization(
        self, analysis: ProgramAnalysis, cand: Any,
    ) -> tuple[dict[str, float], dict[str, Any]]:
        """Fit both drivers, apply the correction in the linear domain, and
        re-solve the trim from the LINEARIZED branch pair — the ordering
        the design doc calls out as structurally defusing #1667's band-
        average trim bias. Returns ``(role_attenuations_db, linearization)``;
        falls back to ``cand.trim_db`` (with a WARNING) when the re-solved
        trim is implausibly far from the raw solve.

        Only called after :meth:`_linearization_eligible` — this method
        assumes both driver responses exist and are adequately repeated;
        it does not re-check. May raise on a fit-engine bug; the caller
        (``_build_candidate``) is responsible for catching that (SF2).

        Side effect: stamps ``self._last_linearization_outcome`` with
        ``"fitted"`` or ``"trim_rejected"`` (SF3) — mirrors
        ``_linearization_eligible``'s own convention; read by
        ``_log_measure_diag``. Also stamps ``self._last_linearized_
        predicted_sum`` with the LINEARIZED-branch VERIFY prediction
        (hardware-validation-caught coherence fix, #1668 PR-D) — the same
        ``W_lin``/``T_lin`` this method's own trim re-solve used, at
        whichever trim this call actually committed to (the sanity-guarded
        ``role_attenuations_db`` return value, not necessarily the re-solved
        ``resolved`` — the correction filters are emitted either way, only
        the trim differs on a rejection). Read by ``_measure_verdict`` to
        override ``self._measure_predicted_sum``.
        """
        woofer_role, tweeter_role = self._woofer.role, self._tweeter.role
        woofer_resp = _driver_response_by_role(analysis, woofer_role)
        tweeter_resp = _driver_response_by_role(analysis, tweeter_role)
        assert woofer_resp is not None and tweeter_resp is not None  # eligibility checked this

        measure_program = self._program_for_phase(PHASE_MEASURE)
        seg_w = measure_program.segment("sweep_w")
        seg_t = measure_program.segment("sweep_t")
        # ProgramSegment.f1_hz/f2_hz are typed float | None (the general
        # ProgramSegment shape also covers non-stimulus/silence segments);
        # __post_init__ guarantees a KIND_SWEEP stimulus segment (which
        # "sweep_w"/"sweep_t" always are) never has either as None. Narrow
        # explicitly for mypy and as a defensive invariant check.
        assert seg_w.f1_hz is not None and seg_w.f2_hz is not None
        assert seg_t.f1_hz is not None and seg_t.f2_hz is not None
        excited_band_hz: dict[str, tuple[float, float]] = {
            woofer_role: (seg_w.f1_hz, seg_w.f2_hz),
            tweeter_role: (seg_t.f1_hz, seg_t.f2_hz),
        }
        responses = {woofer_role: woofer_resp, tweeter_role: tweeter_resp}
        siblings = {woofer_role: tweeter_resp, tweeter_role: woofer_resp}
        mic_tier = str(analysis.mic_tier)

        fits: dict[str, Any] = {}
        corrections: dict[str, np.ndarray] = {}
        for role in (woofer_role, tweeter_role):
            resp = responses[role]
            sigma_db = _compose_sigma_db(
                resp, siblings[role],
                tier=mic_tier, valid_band_hz=excited_band_hz[role],
            )
            envelope = compose_envelope(
                role, resp,
                excited_band_hz=excited_band_hz[role],
                mic_tier=mic_tier,
                driver_class=self._driver_class_by_role.get(role, "unknown"),
                sigma_db=sigma_db,
            )
            fit = fit_driver_linearization(resp, envelope)
            fits[role] = fit
            # COMPLEX (minimum-phase) correction, not a zero-phase magnitude
            # scale (#1667). The emitted biquads rotate phase near their
            # corners and the two-branch summation below is phase-dominated, so
            # a magnitude-only model mispredicts it — measured on JTS3, the
            # zero-phase model mistracked the VERIFY summation by ~2.0 dB
            # (WORSE than the ~1.7 dB of no correction at all) where this
            # complex model tracks to ~0.5 dB. This is the single seam: the
            # complex-corrected branches below feed all three consumers (the
            # trim re-solve, the ripple-optimal scan, and the persisted VERIFY
            # prediction). See complex_correction_response's docstring.
            corrections[role] = complex_correction_response(fit.filters, resp.freqs_hz)

        freqs = woofer_resp.freqs_hz
        W_lin = woofer_resp.complex_tf * corrections[woofer_role]
        T_lin = tweeter_resp.complex_tf * corrections[tweeter_role]

        # Same gating-consistent overlap band the raw trim solve used
        # (program_analysis._build_candidate's own branch_floor_hz clamp —
        # _measure_validity_floor_hz mirrors it), so the comparison below is
        # apples to apples: same band, linearized vs raw branch content.
        lo, hi = overlap_band_hz(
            self._fc_hz, tweeter_sweep_lo_hz=seg_t.f1_hz, woofer_sweep_hi_hz=seg_w.f2_hz,
        )
        branch_floor_hz = _measure_validity_floor_hz(analysis)
        lo_clamped = (
            max(lo, branch_floor_hz)
            if branch_floor_hz is not None and math.isfinite(branch_floor_hz)
            else lo
        )
        trim_w_lin, trim_t_lin_band_average, _lw, _lt = solve_branch_trims(
            freqs, W_lin, T_lin, self._fc_hz, lo_hz=lo_clamped, hi_hz=hi,
        )
        # #1667: ripple-optimal re-solve on the LINEARIZED branch pair, same
        # fix as the raw candidate's own re-solve
        # (program_analysis._build_candidate) — see
        # solve_ripple_optimal_trim's docstring. No separate sanity guard
        # needed here: the wild-trim check below already re-validates this
        # result against the raw candidate's OWN trim, which (after the same
        # #1667 fix, one layer down) is itself ripple-optimal. The effective
        # bound on trim_t_lin is therefore the solver's own +/-window_db
        # (10 dB) scan window around trim_t_lin_band_average combined with
        # the wild-trim check's +/-LINEARIZATION_TRIM_SANITY_MARGIN_DB
        # (6 dB) against the raw candidate below — not a single guard on
        # this call's own seed distance.
        assert analysis.alignment is not None  # MEASURE analyses always carry one
        trim_t_lin, ripple_lin, _seed_lin = solve_ripple_optimal_trim(
            freqs, W_lin, T_lin, self._fc_hz,
            lo_hz=lo_clamped, hi_hz=hi,
            seed_trim_db=trim_t_lin_band_average,
            trim_w_db=trim_w_lin,
            sign=analysis.alignment.polarity_sign,
        )
        resolved = {woofer_role: float(trim_w_lin), tweeter_role: float(trim_t_lin)}

        raw_trim = dict(cand.trim_db)
        wild = any(
            abs(resolved[role] - raw_trim[role]) > LINEARIZATION_TRIM_SANITY_MARGIN_DB
            for role in (woofer_role, tweeter_role)
            if role in raw_trim
        )
        if wild:
            log_event(
                logger, "correction.crossover_v2_linearization_trim_rejected",
                level=logging.WARNING, session_id=self.session_id,
                raw_trim_db={k: round(v, 3) for k, v in raw_trim.items()},
                resolved_trim_db={k: round(v, 3) for k, v in resolved.items()},
                margin_db=LINEARIZATION_TRIM_SANITY_MARGIN_DB,
                # P4 telemetry (2026-07-24 review): the ripple at each trim lets
                # live evidence distinguish "legitimate flatter optimum rejected"
                # from "garbage correctly caught" before anyone widens the guard.
                resolved_ripple_db=round(float(ripple_lin), 3),
                raw_predicted_ripple_db=round(float(cand.predicted_ripple_db), 3),
            )
            role_attenuations_db = raw_trim
            self._last_linearization_outcome = "trim_rejected"  # SF3
        else:
            role_attenuations_db = resolved
            self._last_linearization_outcome = "fitted"  # SF3

        # VERIFY-prediction coherence fix (hardware-validation-caught live
        # finding, #1668 PR-D): the emitted graph carries these SAME W_lin/
        # T_lin correction filters regardless of which branch above ran —
        # the wild-trim guard only ever changes the TRIM, never whether the
        # filters are emitted (``linearization`` below is populated in both
        # cases) — so the persisted VERIFY prediction must be rebuilt from
        # them too, at whichever trim ``role_attenuations_db`` actually ended
        # up holding. Mirrors ``program_analysis._build_candidate``'s own
        # final predicted-sum call exactly: full-grid branches, no
        # residual-delay term (the branches are already in the
        # argmax-referenced frame). Without this, VERIFY compared the
        # correctly-linearized measured summation against a prediction still
        # built from the raw branches — a deterministic mismatch equal to
        # the filters' own in-band response (measured live on JTS3:
        # 1.688-1.699 dB across three attempts, against the 1.5 dB
        # tolerance).
        predicted_lin = predicted_branch_sum(
            W_lin, T_lin,
            role_attenuations_db[woofer_role], role_attenuations_db[tweeter_role],
            analysis.alignment.polarity_sign,
        )
        self._last_linearized_predicted_sum = (
            freqs, 20.0 * np.log10(np.maximum(np.abs(predicted_lin), 1e-12)),
        )

        linearization = {role: fit.to_dict() for role, fit in fits.items()}
        return role_attenuations_db, linearization


# --------------------------------------------------------------------------- #
# capture plan + session spec (§5.7, auto-advance policy §5.2)
# --------------------------------------------------------------------------- #

# Phone-side recording margin around each program (lead + tail), presentation /
# locator-window data — never a hard deadline (the session runner's timeout_s
# stays the backstop).
CAPTURE_ENTRY_MARGIN_MS = 2000
# The cancelable auto-advance countdown between an accepted CHECK and MEASURE
# (§5.2 — one tap per session is the design; the countdown protects validity
# because a user returning to the phone cold is the likeliest mic-displacement
# event). PROVISIONAL pending W6.
AUTO_ADVANCE_COUNTDOWN_S = 5

# Auto-advance policy vocabulary carried in the per-entry ``screen`` field
# (page policy, not a protocol change — the field is opaque to the schema).
AUTO_ADVANCE_TAP = "tap"            # requires the user's tap (first capture)
AUTO_ADVANCE_COUNTDOWN = "countdown"  # auto-begins behind a cancelable countdown
AUTO_ADVANCE_ON_APPLY = "on_apply"  # armed by the apply-complete host event

# PROVISIONAL (W6.10 fold-in): phone-inactivity budget for the very FIRST begin
# of a v2 session (before any capture). The microphone-check screen's placement
# instructions alone legitimately take longer than the general 120 s
# ``DEFAULT_TIMEOUT_S`` to read — Chrome round 1 collapsed here — so the v2 runner
# widens only this first window. Every later window keeps the tight per-phase
# arm/upload backstop; re-derive from W6 bench observation.
V2_FIRST_BEGIN_TIMEOUT_S = 300.0


def _program_duration_ms(program: ExcitationProgram) -> int:
    return int(round(program.total_samples / program.sample_rate_hz * 1000))


def build_v2_capture_plan(
    roles_bands: Sequence[RoleBand],
    fc_hz: float,
) -> Any:
    """The 3-entry heterogeneous CapturePlan (check / measure / verify, §5.7).

    Entry durations derive from the composed programs (MEASURE sized from a
    nominal gain plan — sweep/gap lengths are gain-independent, so the duration
    is exact even before CHECK's solve) plus a lead/tail margin; each entry's
    ``screen`` carries the phase prompt AND the §5.2 auto-advance policy:
    CHECK is the session's one required tap, MEASURE auto-advances behind a
    visible cancelable countdown, VERIFY arms on the apply-complete host event.
    """
    from jasper.capture_relay.spec import (
        MAX_CAPTURE_PLAN_ATTEMPTS,
        CapturePlan,
        CapturePlanEntry,
    )

    roles = tuple(roles_bands)
    # courtesy_prelude=COURTESY_PRELUDE_ENABLED on every composed program below
    # (issue #1677): this is the phone's DURATION BUDGET, so it must agree with
    # what the conductor's own _compose_*_program methods actually play, or the
    # phone stops recording before the real (prelude-lengthened) program ends.
    check = build_check_program(roles, courtesy_prelude=COURTESY_PRELUDE_ENABLED)
    nominal_gains = {rb.role: BASE_STIMULUS_PEAK_DBFS for rb in roles}
    measure = build_measure_program(
        nominal_gains, roles,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=COURTESY_PRELUDE_ENABLED,
    )
    verify = build_verify_program(
        fc_hz,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=COURTESY_PRELUDE_ENABLED,
    )
    entries = (
        CapturePlanEntry(
            index=0,
            kind_label="check",
            duration_ms=_program_duration_ms(check) + CAPTURE_ENTRY_MARGIN_MS,
            screen={
                "title": "Microphone check",
                "body": (
                    "Place the phone about 1 m in front of the speaker at "
                    "tweeter height, then tap Start. Stay quiet — JTS listens "
                    "to the room first."
                ),
                "auto_advance": AUTO_ADVANCE_TAP,
            },
        ),
        CapturePlanEntry(
            index=1,
            kind_label="measure",
            duration_ms=_program_duration_ms(measure) + CAPTURE_ENTRY_MARGIN_MS,
            screen={
                "title": "Measuring",
                "body": "Keep the phone still — measuring both drivers.",
                "auto_advance": AUTO_ADVANCE_COUNTDOWN,
                "countdown_s": str(AUTO_ADVANCE_COUNTDOWN_S),
                "cancelable": "1",
            },
        ),
        CapturePlanEntry(
            index=2,
            kind_label="verify",
            duration_ms=_program_duration_ms(verify) + CAPTURE_ENTRY_MARGIN_MS,
            screen={
                "title": "Applying",
                # Fallback only — the live hold shows the CaptureBeginDeferred
                # deferral's own user_message instead (authorize_begin below),
                # which wins whenever a hold is actually in progress.
                "body": (
                    "JTS is applying the measured crossover to your speaker."
                ),
                "auto_advance": AUTO_ADVANCE_ON_APPLY,
                # The phone's END screen once every capture (including this
                # VERIFY) completes (capture-page/js/main.js's
                # renderPlanAllDone) — owner ruling, 2026-07-20: state the
                # outcome plainly and point at the speaker page for
                # undo/compare, rather than the shared "All measurements
                # done" generic copy every other capture-plan flow gets.
                "done_title": "Your speaker is tuned",
                "done_body": (
                    "Verified and applied. Manage or undo on the speaker "
                    "page."
                ),
            },
        ),
    )
    return CapturePlan(
        capture_target=CAPTURE_PLAN_TARGET,
        max_attempts=MAX_CAPTURE_PLAN_ATTEMPTS,
        schema_version=2,
        entries=entries,
    )


def build_v2_verify_capture_plan(fc_hz: float) -> Any:
    """A 1-entry verify-only plan for the §5.2 re-verify re-arm session.

    Used by ``/crossover/v2/verify`` after a VERIFY fail/inconclusive when the
    original session has died: the household explicitly chose "Try again," so
    the single entry requires the tap (no countdown — apply already happened).
    The hosting conductor maps relay index 1 → VERIFY via ``index_phase_map``.
    """
    from jasper.capture_relay.spec import (
        MAX_CAPTURE_PLAN_ATTEMPTS,
        CapturePlan,
        CapturePlanEntry,
    )

    verify = build_verify_program(
        fc_hz,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=COURTESY_PRELUDE_ENABLED,
    )
    entry = CapturePlanEntry(
        index=0,
        kind_label="verify",
        duration_ms=_program_duration_ms(verify) + CAPTURE_ENTRY_MARGIN_MS,
        screen={
            "title": "Verify",
            "body": (
                "Keep the phone where it was for the measurement, then tap "
                "Verify."
            ),
            "auto_advance": AUTO_ADVANCE_TAP,
        },
    )
    return CapturePlan(
        capture_target=1,
        max_attempts=MAX_CAPTURE_PLAN_ATTEMPTS,
        schema_version=2,
        entries=(entry,),
    )


def build_v2_verify_session_spec(
    fc_hz: float,
    *,
    acknowledgement_binding: str,
    **spec_kwargs: Any,
) -> Any:
    """The relay v3 spec for a verify-only re-arm session (§5.2 re-verify)."""
    from jasper.capture_relay.spec import build_crossover_sweep_spec

    plan = build_v2_verify_capture_plan(fc_hz)
    return build_crossover_sweep_spec(
        driver_label="crossover verification",
        driver_role="summed",
        acknowledgement_binding=acknowledgement_binding,
        stimulus_duration_ms=plan.entries[0].duration_ms,
        capture_plan=plan,
        **spec_kwargs,
    )


def build_v2_session_spec(
    roles_bands: Sequence[RoleBand],
    fc_hz: float,
    *,
    acknowledgement_binding: str,
    **spec_kwargs: Any,
) -> Any:
    """One relay v3 session spec spanning all three v2 phases (§5.7).

    Rides the existing ``build_crossover_sweep_spec`` (same kind, transport,
    and placement-acknowledgement machinery) with the 3-entry plan attached;
    the summed fixed-on-axis placement copy is the closest shipped match to the
    v2 single mic position (the per-entry screens carry the v2 prompts). The
    spec-level stimulus duration is the longest entry so the per-capture
    deadline covers every phase.
    """
    from jasper.capture_relay.spec import build_crossover_sweep_spec

    plan = build_v2_capture_plan(roles_bands, fc_hz)
    longest_ms = max(entry.duration_ms for entry in plan.entries)
    return build_crossover_sweep_spec(
        driver_label="crossover",
        driver_role="summed",
        acknowledgement_binding=acknowledgement_binding,
        stimulus_duration_ms=longest_ms,
        capture_plan=plan,
        **spec_kwargs,
    )


# --------------------------------------------------------------------------- #
# production playback seams (binds W2's play_program to the real DSP boundary)
# --------------------------------------------------------------------------- #


def bind_program_playback_seams(
    cam: Any,
    *,
    bundle_dir: str,
    artifact: Any,
    config_dir: str,
    program: ExcitationProgram,
    wav_path: str,
    topology: Any,
    safety_profile: Mapping[str, Any],
    role_targets: Mapping[str, str],
    session_volume_db: float,
    declared_sensitivities: Mapping[str, float] | None = None,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """The real CamillaController-backed seams for :func:`play_program` (W2's
    open wiring question, answered here).

    Returns the keyword mapping ``play_program(program, program_graph_yaml=...,
    session_volume_plan=..., **bind_program_playback_seams(...))`` consumes:

    * ``read_current_config_path`` — ``cam.get_config_file_path`` (the persisted
      statefile boot anchor, the restore target).
    * ``load_program_graph`` — INLINE ``cam.set_active_config_raw`` (CamillaDSP
      ``SetConfig``): applies the program graph WITHOUT repointing the persisted
      statefile, preserving the crash-recovery-MUTED structural invariant
      exactly as :func:`jasper.active_speaker.commission_wiring.commission_load_config`
      documents. A crash mid-program reboots onto the staged anchor, never the
      program graph.
    * ``restore_graph`` — reads the entry config path's bytes and re-applies
      them inline (same SetConfig transport; the statefile stays untouched).
    * ``play_wav`` — the verified-WAV source
      (:func:`jasper.active_speaker.program_playback.verified_program_aplay`):
      sha256-bound bytes through the stable-fd aplay path to
      ``correction_substream``.
    * ``readmit`` — :func:`jasper.active_speaker.program_admission.readmit_program_from_wav`
      from a FRESH byte readback (the play-time gate).
    * ``writer_lock`` — :func:`jasper.dsp_apply.dsp_writer_lock` on the shared
      generated-config dir, so the program load/restore serializes with every
      other DSP writer.

    NOT hardware-validated yet — W6 exercises this binding end-to-end on JTS3;
    until then it is the single place the real transport is named, and every
    orchestration test injects fakes instead.
    """
    from pathlib import Path

    from jasper.dsp_apply import dsp_writer_lock

    from .program_admission import readmit_program_from_wav
    from .program_playback import verified_program_aplay

    async def _read_current_config_path() -> str | None:
        return await cam.get_config_file_path(best_effort=False)

    async def _load_program_graph(program_graph_yaml: str) -> bool:
        return await cam.set_active_config_raw(program_graph_yaml, best_effort=False)

    async def _restore_graph(entry_config_path: str) -> bool:
        text = Path(entry_config_path).read_text(encoding="utf-8")
        return await cam.set_active_config_raw(text, best_effort=False)

    async def _play_wav() -> Any:
        return await verified_program_aplay(bundle_dir, artifact, timeout_s=timeout_s)

    async def _readmit() -> Any:
        # ``declared_sensitivities`` MUST match what the conductor composed
        # against: readmission re-resolves every cap, so a program composed at
        # the W6.5-derived HF ceiling would be refused here at the legacy one
        # if the mapping were dropped on this side.
        return readmit_program_from_wav(
            program,
            wav_path,
            topology=topology,
            safety_profile=safety_profile,
            role_targets=role_targets,
            session_volume_db=session_volume_db,
            declared_sensitivities=declared_sensitivities,
        )

    return {
        "read_current_config_path": _read_current_config_path,
        "load_program_graph": _load_program_graph,
        "restore_graph": _restore_graph,
        "play_wav": _play_wav,
        "readmit": _readmit,
        "writer_lock": lambda: dsp_writer_lock(
            config_dir, source="crossover_v2_program"
        ),
    }


# --------------------------------------------------------------------------- #
# session-volume lifecycle (one SessionVolumePlan per session, §5.5)
# --------------------------------------------------------------------------- #


def derive_session_volume_db(
    safety_profile: Mapping[str, Any],
    target_fingerprints: Sequence[str],
    *,
    declared_sensitivities: Mapping[str, float] | None = None,
) -> float:
    """The fixed session measurement volume — the SSOT derivation (§5.5).

    Thin pass-through to
    :func:`jasper.active_speaker.session_volume_plan.session_measurement_volume_db`
    so the conductor and its callers reach the one derivation path (least-
    sensitive driver reaches the reference level; more-sensitive drivers
    attenuate down digitally). Kept here so the flow imports one module.
    ``declared_sensitivities`` rides through so the caps feeding ``max(caps)``
    are the same W6.5-derived caps admission enforces.
    """
    from .session_volume_plan import session_measurement_volume_db

    return session_measurement_volume_db(
        safety_profile,
        target_fingerprints,
        declared_sensitivities=declared_sensitivities,
    )


async def open_measurement_volume(
    plan: Any,
    *,
    safety_profile: Mapping[str, Any],
    target_fingerprints: Sequence[str],
    set_main_volume_db: Any,
    get_main_volume_db: Any,
    declared_sensitivities: Mapping[str, float] | None = None,
) -> Any:
    """Open the one session volume for a fresh v2 session (§5.5).

    Gates on ``plan.needs_recovery`` FIRST (not ``unresolved_volume_safety``
    alone — the W2 gate ruling: a crash-hydrated active plan needs draining but
    surfaces no unresolved payload), then derives the fixed volume via the SSOT
    and opens the plan. Refuses to open over a plan that needs recovery.
    """
    if plan.needs_recovery:
        raise CrossoverV2FlowError(
            "the session volume needs recovery; drain it before opening a session"
        )
    volume_db = derive_session_volume_db(
        safety_profile,
        target_fingerprints,
        declared_sensitivities=declared_sensitivities,
    )
    return await plan.open(volume_db, set_main_volume_db, get_main_volume_db)


async def abandon_measurement_volume(
    plan: Any, *, set_main_volume_db: Any, get_main_volume_db: Any,
) -> Any:
    """Session-death observation hook — drain the restore-once path (§5.5).

    The flow wires the relay session's death (TTL expiry / failure / explicit
    stop) to this so a walked-away user can never leave the speaker pinned at
    measurement volume. Delegates to the plan's ``abandon`` (the same
    fail-closed latch trio ``close`` uses).
    """
    return await plan.abandon(set_main_volume_db, get_main_volume_db)


__all__ = [
    "CrossoverV2Conductor",
    "CrossoverV2FlowError",
    "bind_program_playback_seams",
    "build_v2_capture_plan",
    "build_v2_session_spec",
    "build_v2_verify_capture_plan",
    "build_v2_verify_session_spec",
    "derive_session_volume_db",
    "open_measurement_volume",
    "abandon_measurement_volume",
    "V2ConductorSnapshot",
    "V2FlowSeams",
    "PhaseVerdict",
    "ReasonSpec",
    "REASON_REGISTRY",
    "TRANSIENT_AUTO_RETRY_CODES",
    "PHASE_CHECK",
    "PHASE_MEASURE",
    "PHASE_APPLYING",
    "PHASE_VERIFY",
    "PHASE_DONE",
    "CAPTURE_PHASES",
    "CAPTURE_PLAN_TARGET",
    "V2_FIRST_BEGIN_TIMEOUT_S",
    "ALIGNMENT_CONFIDENCE_TRUST_FLOOR",
    "MEASURE_PREDICTED_RIPPLE_CEILING_DB",
    "SWEEP_SCHEDULE_RESIDUAL_CEILING_MS",
    "SWEEP_LOCATE_CONFIDENCE_FLOOR",
    "VERIFY_PILOT_TRANSFER_STEP_CEILING_DB",
    "alignment_to_candidate_fields",
    "back_off_gain",
    "TEMPLATE_SILENT_AUTO_RETRY",
    "TEMPLATE_FIX_AND_RETRY",
    "TEMPLATE_HARD_STOP",
    "TEMPLATE_SESSION_RESTART",
    "TEMPLATE_VERIFY_FAIL",
    "TEMPLATE_VOLUME_RECOVERY",
    "REASON_AGC_BEHAVIORAL_FAIL",
    "REASON_NOISY_ROOM_LINEARITY",
    "REASON_SNR_FLOOR",
    "REASON_CHANNEL_MAP_MISMATCH",
    "REASON_CLIPPED",
    "REASON_DRIFT_BASELINES_DISAGREE",
    "REASON_DELAY_EXCEEDS_SEARCH_WINDOW",
    "REASON_LOCATE_FAILED",
    "REASON_RELAY_TIMEOUT",
    "REASON_VOLUME_UNRESOLVED",
    "REASON_PROGRAM_UNPLAYABLE",
    "REASON_INTERNAL_ERROR",
    "REASON_VERIFY_OUT_OF_TOLERANCE",
    "REASON_VERIFY_INCONCLUSIVE",
    "REASON_VERIFY_LEVEL_SHIFT",
    "REASON_LOW_ALIGNMENT_CONFIDENCE",
    "REASON_APPLY_FAILED",
    "REASON_USER_STOPPED",
    "REASON_REVIEW_HOLD_TIMEOUT",
]
