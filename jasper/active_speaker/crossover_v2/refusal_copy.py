# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What the household is told when a round refuses, and the verdict that says it.

The one module here that owns household-facing copy rather than a decision:
the codes, the templates, the :data:`REASON_REGISTRY` binding a code to its
sentence and retry budget, the selectors that pick between two sentences for
one code, and :class:`PhaseVerdict`. :data:`SCREEN_KIND_REASONS` covers
:data:`~.capture_dispatch.CAPTURE_SCREEN_KINDS` exactly and names only
:data:`REASON_REGISTRY` codes (pinned in ``tests/test_crossover_v2_spatial.py``),
so a new rung cannot ship without a household sentence. Every sibling answers with a *kind* and
never renders a sentence. Where this vocabulary belongs is still open (#2390).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

from jasper.active_speaker.delta_probe import (
    VERDICT_LEVEL_DEPENDENT_SHORTFALL,
    VERDICT_MODEL_ERROR,
    VERDICT_SPATIALLY_COSTLY,
)
from jasper.log_event import log_event

from . import capture_dispatch as _dispatch
from . import spatial as _spatial
from .spatial import GEOMETRY_RETRY_POSITIONS

logger = logging.getLogger(__name__)


# The four generic screen templates, each parameterized by reason copy.
TEMPLATE_SILENT_AUTO_RETRY = "silent_auto_retry"
TEMPLATE_FIX_AND_RETRY = "fix_and_retry"
TEMPLATE_HARD_STOP = "hard_stop"
TEMPLATE_SESSION_RESTART = "session_restart"
# Two special screens (§5.2), not among the four generic templates.
TEMPLATE_VERIFY_FAIL = "verify_fail"
TEMPLATE_VOLUME_RECOVERY = "volume_recovery"

# Reason codes (internal — never a bare code reaches the household; the envelope
# renders each through its template copy).
REASON_AGC_BEHAVIORAL_FAIL = "agc_behavioral_fail"
# The same pilot mismatch ``REASON_AGC_BEHAVIORAL_FAIL`` names, caused by a
# loud ambient burst rather than the phone's AGC. ``_consume_check``
# distinguishes the two on the CHECK gain solve's own ``gain_plan.
# snr_floor_ok``, computed against this capture's ambient bands independent of
# the linearity outcome.
REASON_NOISY_ROOM_LINEARITY = "noisy_room_linearity"
# The same discriminator, for the phases CHECK's evidence cannot speak for:
# `analysis.pilot_snr_ok` False means the quiet pilot did not clear the room's
# own in-band floor by enough to trust ANY level comparison drawn from the
# pair — a statement about the room, not the microphone.
# `_pilot_observations` reports ``linearity_ok`` as None whenever the SNR guard
# fails, so every verdict below checks this BEFORE
# `REASON_AGC_BEHAVIORAL_FAIL`.
REASON_PILOT_LEVEL_COLLAPSE = "pilot_level_collapse"
REASON_SNR_FLOOR = "snr_floor"
REASON_CHANNEL_MAP_MISMATCH = "channel_map_mismatch"
# The analyzer could not decide WHICH scheduled tone a capture's first arrival
# was, so it cannot say which driver played what. Retriable, and its copy names
# the recording rather than the speaker: the alternative,
# `REASON_CHANNEL_MAP_MISMATCH`, is a hard stop telling a household to open its
# speaker, and the evidence cannot support that. Ladder rung:
# `capture_dispatch.SCREEN_ANCHOR_AMBIGUOUS`.
REASON_ANCHOR_AMBIGUOUS = "anchor_ambiguous"
REASON_CLIPPED = "clipped"
REASON_DRIFT_BASELINES_DISAGREE = "drift_baselines_disagree"
REASON_DELAY_EXCEEDS_SEARCH_WINDOW = "delay_exceeds_search_window"
REASON_LOCATE_FAILED = "locate_failed"
REASON_CAPTURE_TIMEOUT = "capture_timeout"
REASON_VOLUME_UNRESOLVED = "volume_unresolved"
# The play seam refused or failed the program (safety re-admission over-cap, a
# graph-restore failure, a session program error) — distinct from a capture
# transport death (``capture_timeout``). Terminal: a play-time refusal is a bug,
# a tampered readback, or a genuinely infeasible profile.
REASON_PROGRAM_UNPLAYABLE = "program_unplayable"
# The main fader was not at the volume this session declared when a stimulus
# was about to play, and re-asserting it could not be proven. The program was
# admissible; the SPEAKER's level was not the one it was admitted against.
# Terminal: the re-assert has already been tried and could not be confirmed.
# NOT ``volume_unresolved``, whose subject is the RESTORE path.
REASON_MEASUREMENT_VOLUME_DRIFT = "measurement_volume_drift"
# The program PLAYED; the offline evidence math refused. §4.2 divides the
# emitted measurement protection back out of the capture, and on a
# candidate-required bin that division is inadmissible when the protection
# attenuates more than 12 dB or the recovery would exceed 12 dB. Deterministic,
# so terminal. The offending slug rides out in the refusal detail.
REASON_PROTECTION_NOT_SEPARABLE = "protection_not_separable"
# Sibling for the OTHER conditioning branch: `abs(P) < floor` does not involve
# `C`, so "change the crossover frequency" cannot clear it.
REASON_PROTECTION_SWEEP_TOO_LOW = "protection_sweep_too_low"
# The ONE program refusal that is neither unexpected nor about levels: a
# deterministic, one-edit-away state, not a level ceiling the speaker could not
# meet. Terminal, because deterministic. The states that reach it are ``stale``
# and ``malformed``. The SLUG keeps its wire name — a stable identifier
# shipping in ``state["failure"]``, the phone envelope, and the journal.
REASON_PROGRAM_PROFILE_NOT_CONFIRMED = "program_profile_not_confirmed"
# Its two siblings, told apart by the session-open pre-flight, which holds the
# full ``DriverSafetyProfileEvaluation``: ``missing`` has no profile at all and
# no ``/sound/`` safety callout to review, ``incomplete`` has declared values
# that are missing or do not line up, so a save rebuilds the same profile.
# Neither has a ``ProgramAdmissionRefusal`` counterpart — the play-seam
# vocabulary carries one ``PROFILE_NOT_CONFIRMED`` slug for all three.
REASON_PROGRAM_PROFILE_MISSING = "program_profile_missing"
REASON_PROGRAM_PROFILE_INCOMPLETE = "program_profile_incomplete"
# The session-open shape gate: the walk handles a 1-way passive main or a
# 2-way, and this speaker is neither. Terminal — no household action clears it.
REASON_SPEAKER_SHAPE_UNSUPPORTED = "speaker_shape_unsupported"
# Its sibling one gate later: the shape is walkable, but live status carries no
# measurement target for every role it declares. The roles reach the journal.
REASON_MEASUREMENT_TARGETS_MISSING = "measurement_targets_missing"

# Any OTHER host-side fault the session runner's catch-all cleanup arm caught.
# The seams raise open-endedly (CamillaUnavailable is a bare Exception,
# analyze/emit raise ValueError/RuntimeError, the held measurement window
# raises MeasurementWindowError), so an enumerated except list is how failures
# escape with the volume active and the phone frozen. Terminal.
REASON_INTERNAL_ERROR = "internal_error"
REASON_VERIFY_OUT_OF_TOLERANCE = "verify_out_of_tolerance"
# The SAME out-of-tolerance observation, once a second graded attempt has shown
# it REPEATS. When consecutive attempts agree inside the instrument's own
# repeat floor the mismatch is a FINDING about the speaker, and every further
# retry re-measures the same applied graph into the same answer. Terminal
# (budget 0, so ``NON_RETRIABLE_CODES``), on the same "deterministic ⇒
# terminal" rule the two codes above state. Renders through the SAME
# ``verify_fail`` template as its siblings — one more parameterization of that
# screen, not a new screen.
REASON_VERIFY_DETERMINISTIC_MISMATCH = "verify_deterministic_mismatch"
# §5.2's "inconclusive — re-verify" verdict: VERIFY's own detected first
# reflection forced a shorter gate than MEASURE's, so the overlay difference is
# not evidence about driver alignment.
REASON_VERIFY_INCONCLUSIVE = "verify_inconclusive"
# A distinct VERIFY outcome: the recording chain drifted between VERIFY
# attempts, not the speaker going out of tolerance.
REASON_VERIFY_LEVEL_SHIFT = "verify_level_shift"
# The applied result tracks the model but does NOT meet the candidate's own
# crossover target through the handoff — the case
# ``REASON_VERIFY_OUT_OF_TOLERANCE`` structurally cannot catch, since it grades
# measured-vs-model and a defect present in BOTH sides cancels.
REASON_VERIFY_CROSSOVER_REGION = "verify_crossover_region"
# The measured delay is one physics rules out for this geometry — a GCC
# estimator returning a CONFIDENTLY WRONG lag, observed on hardware at −631 us
# against a declared [50, 300] us search bound. The confidence half of the code
# this replaced is provenance rather than a gate
# (``docs/measurement-loop-doctrine.md`` §4) and rides the receipt.
REASON_DELAY_IMPLAUSIBLE = "delay_implausible"
# The apply transaction came back blocked or raised.
# ``_persist_terminal_failure`` scopes its §5.6 evidence reset away from this
# code: an apply failure says nothing about the mic position.
REASON_APPLY_FAILED = "apply_failed"
# A deliberate phone Stop (CaptureAborted, abort_reason == "stopped") is not a
# transport death — see the catch-all's exception classification in
# jasper.web.correction_crossover_v2.
REASON_USER_STOPPED = "user_stopped"
# The deferred apply/"review" hold (CaptureBeginDeferred "awaiting_apply")
# expired before an apply completed. Distinct from a transport death
# (capture_timeout) and a deliberate phone Stop (user_stopped). Retained but
# unreached: no shipped session holds for an apply.
REASON_REVIEW_HOLD_TIMEOUT = "review_hold_timeout"
# The position gate's three refusals, reachable by EITHER gated shape
# (``TIER_REMOTE`` and a hand-walked round on the WIRED capture source), so the
# copy names neither mover. All three TEMPLATE_SESSION_RESTART: no retry can
# help once the session has been torn down.
#
#   position_hold_expired  — nothing reported the microphone in place before
#                            REMOTE_POSITION_HOLD_BUDGET_S.
#   position_target_missing— a plan entry carried no target angle, so the gate
#                            refused rather than measure an unknown position.
#   session_ceiling_expired— the WHOLE walk outlived the session's wall-clock
#                            ceiling while a hold was pending, no single hold
#                            having expired. The per-hold budget catches a
#                            driver that STOPS; this one that is merely slow.
REASON_POSITION_HOLD_EXPIRED = "position_hold_expired"
REASON_POSITION_TARGET_MISSING = "position_target_missing"
REASON_SESSION_CEILING_EXPIRED = "session_ceiling_expired"
# The geometry-locked retake asks for a pose PAST the walk — 75 cm out, and on
# its second rung 75 cm out AND above mark height. No GATED session can serve
# it: an external positioner swings on one horizontal axis at a fixed radius.
REASON_GEOMETRY_RETAKE_UNREACHABLE = "geometry_retake_unreachable"
# The pre-apply cloud closed with `spatial_combine.assess_geometry` reporting
# `locked` — every position's echo estimate landed on the same tau, so the
# nulls are not moving and spatial averaging cannot fill them. Not a bad
# capture. The group asks for that position again from a wider spot, at most
# ``GEOMETRY_RETRY_POSITIONS`` times, then proceeds with the verdict recorded
# rather than blocking on a defect no mic move can decorrelate.
REASON_CLOUD_GEOMETRY_LOCKED = "cloud_geometry_locked"
# Delta-probe verdicts. These fire AFTER the apply — what the post-apply sweep
# found — so each rolls the correction back before it names itself. Every
# reader tolerates a persisted literal with no registry row
# (``_failure_history_note`` in ``crossover_envelope_v2`` reads with ``.get``).
#
# The correction did not do what its own filters said it would: a chain defect,
# the shelf realized at a Q the fit never modelled being the archetype.
REASON_CORRECTION_MODEL_ERROR = "correction_model_error"
# The correction's shape landed but its depth did not — the driver delivered
# materially less level than it was asked for. A compression diagnostic.
REASON_CORRECTION_LEVEL_SHORTFALL = "correction_level_shortfall"
# The correction tracked at the measuring spot and made the room LESS even
# everywhere else: it fitted one position's interference rather than the
# speaker. The remedy is placement, not a different filter.
REASON_CORRECTION_SPATIALLY_COSTLY = "correction_spatially_costly"
# The probe found a defect AND the automatic rollback could not run (no
# rollback binding, a refused restore, or a seam that raised). The correction
# is therefore STILL APPLIED and the copy has to say so.
REASON_CORRECTION_ROLLBACK_FAILED = "correction_rollback_failed"
# The correction was applied, MEASURED at the same mark with the same program,
# and the speaker is measurably worse than it was before — so it came back off.
# Distinct from the three delta-probe codes, which say the graph did not do
# what its own filters commanded: here the graph did exactly what it was told
# and the room liked it less.
REASON_CORRECTION_MEASURED_REGRESSION = "correction_measured_regression"
# The fail-closed boost: the benefit could not be measured and the applied
# intervention puts energy INTO a driver. An unverified cut can wait for a
# household to decide; an unverified boost cannot, so it comes off.
REASON_CORRECTION_UNPROVEN_BOOST = "correction_unproven_boost"
# The safety row: the post-apply sweep measured the applied graph putting out
# MORE than it declared — a commanded boost realized above its bound, an
# uncommanded level shift in the LOUD direction, or a capture that clipped — so
# it came off. The only cause on this list about output rather than accuracy.
#
# ONE row rather than three hazard-specific ones: the action is identical in
# all three cases and the specific hazard is on the round's own record (the
# safety verdict's reason, in the receipt's ``round_axes`` and the journal).
REASON_CORRECTION_UNSAFE_RESULT = "correction_unsafe_result"
# The untrusted row, for an intervention that puts no energy in. Its boosted
# sibling is REASON_CORRECTION_UNPROVEN_BOOST, whose copy leans on "and it
# turns some parts up" — false for a cut-only correction.
REASON_CORRECTION_UNVERIFIABLE_RESULT = "correction_unverifiable_result"


def round_restore_reason(cause: str) -> str:
    """Adoption cause → the code a SUCCESSFUL round restore surfaces.

    The three SAFETY causes share :data:`REASON_CORRECTION_UNSAFE_RESULT`; the
    four EVIDENCE-TRUST causes share
    :data:`REASON_CORRECTION_UNVERIFIABLE_RESULT` unless the applied
    intervention was boosted, in which case ``decide_adoption`` has already
    substituted ``ADOPTION_UNPROVEN_BOOST``; a measured regression keeps its
    own. A delta-probe rollback class carries its verdict in a composite cause
    (``delta_probe_rollback_class:<verdict>``) and keeps the probe's own
    sentence through :data:`DELTA_PROBE_REASON_BY_VERDICT`.

    Anything unlisted falls back to the unverifiable code — the weakest true
    statement available for "the round asked for a restore". The mapping is
    exhaustive and pinned by a test, so the fallback is a floor.

    A function with a lazy import rather than a module-level dict, because
    :mod:`~jasper.active_speaker.crossover_v2.verification` reaches
    :mod:`~jasper.active_speaker.flat_spec`.
    """
    from jasper.active_speaker.crossover_v2.verification import (
        ADOPTION_MEASURED_REGRESSION,
        ADOPTION_PROBE_ROLLBACK_CLASS,
        ADOPTION_UNPROVEN_BOOST,
        CAPTURE_INTEGRITY_FAILED,
        CAPTURE_INTEGRITY_UNAVAILABLE,
        REALIZATION_NO_COMPARATOR,
        REALIZATION_NO_TRACKING,
        SAFETY_BOOST_OVER_DECLARED_BOUND,
        SAFETY_CLIPPED_CAPTURE,
        SAFETY_UNCOMMANDED_LEVEL_LOUDER,
    )

    prefix, _, probe_verdict = cause.partition(":")
    if prefix == ADOPTION_PROBE_ROLLBACK_CLASS and probe_verdict:
        return DELTA_PROBE_REASON_BY_VERDICT.get(
            probe_verdict, REASON_CORRECTION_UNVERIFIABLE_RESULT,
        )

    return {
        ADOPTION_MEASURED_REGRESSION: REASON_CORRECTION_MEASURED_REGRESSION,
        ADOPTION_UNPROVEN_BOOST: REASON_CORRECTION_UNPROVEN_BOOST,
        SAFETY_BOOST_OVER_DECLARED_BOUND: REASON_CORRECTION_UNSAFE_RESULT,
        SAFETY_UNCOMMANDED_LEVEL_LOUDER: REASON_CORRECTION_UNSAFE_RESULT,
        SAFETY_CLIPPED_CAPTURE: REASON_CORRECTION_UNSAFE_RESULT,
        CAPTURE_INTEGRITY_FAILED: REASON_CORRECTION_UNVERIFIABLE_RESULT,
        CAPTURE_INTEGRITY_UNAVAILABLE: REASON_CORRECTION_UNVERIFIABLE_RESULT,
        REALIZATION_NO_TRACKING: REASON_CORRECTION_UNVERIFIABLE_RESULT,
        REALIZATION_NO_COMPARATOR: REASON_CORRECTION_UNVERIFIABLE_RESULT,
    }.get(cause, REASON_CORRECTION_UNVERIFIABLE_RESULT)


#: Delta-probe verdict → the reason code its rollback surfaces. Exhaustive
#: over :data:`delta_probe.DELTA_PROBE_ROLLBACK_VERDICTS`, pinned by a test
#: written against the NON-MATCHED set: a non-matched verdict that is not here
#: must prove it reaches a household some other way, never merely by being
#: absent from a rollback list.
DELTA_PROBE_REASON_BY_VERDICT: Mapping[str, str] = {
    VERDICT_MODEL_ERROR: REASON_CORRECTION_MODEL_ERROR,
    VERDICT_LEVEL_DEPENDENT_SHORTFALL: REASON_CORRECTION_LEVEL_SHORTFALL,
    VERDICT_SPATIALLY_COSTLY: REASON_CORRECTION_SPATIALLY_COSTLY,
}


def verify_inconclusive_cause(
    code: str | None, reflection_measured: bool | None,
) -> str:
    """WHY a verify check could not settle, as one household clause.

    THE single writer of that clause: it renders on the verify_fail screen's
    reason copy and on the done screen's ungraded verdict, and two paraphrases
    is how the bug this fixes stayed invisible.

    Two things produce the "inconclusive" outcome:

    * ``REASON_VERIFY_INCONCLUSIVE`` — VERIFY's own gate came out SHORTER than
      MEASURE's, so the two captures cannot be compared like for like. WHY the
      window is short is a separate fact: ``reflection_measured``, from
      :attr:`~jasper.audio_measurement.gate_disclosure.GateDisclosure.gated_anything`,
      the single owner of "is the reflections claim true here".
    * ``REASON_VERIFY_LEVEL_SHIFT`` — the recording chain moved between
      attempts; no reflection and no window are involved. It reaches the DONE
      screen's copy only, which keys on the coarse outcome rather than the
      code.

    The two unknowns get different answers. ``code=None`` establishes nothing,
    so the clause is EMPTY. ``reflection_measured=None`` collapses into the
    no-reflection-claim branch, because the code alone already establishes the
    observation; emptying it would leave
    :func:`verify_inconclusive_message` reading "The check was inconclusive —
    . Re-verify to try again."

    Returned without terminal punctuation: the caller owns the sentence.
    """
    if code == REASON_VERIFY_LEVEL_SHIFT:
        # Same vocabulary as that code's own ReasonSpec below: one cause must
        # not have two names depending on which screen is being read.
        return "the microphone's levels changed between measurements"
    if code != REASON_VERIFY_INCONCLUSIVE:
        return ""
    if reflection_measured:
        # The ONE state where blaming a reflection is true.
        return (
            "a reflection reached the microphone sooner than it did during "
            "tuning, so there was less of the sound to compare"
        )
    # Reflection NOT measured, or not recorded. Both render the observation the
    # rule made and stop: a window capped at the search ceiling proves nothing
    # about reflections. The precise gate state is disclosed in expert details
    # by ``gate_disclosure.describe_gate``.
    return "this measurement had less usable sound to compare than the tuning did"


def verify_inconclusive_diagnosis(reflection_measured: bool | None) -> str:
    """What VERIFY established, without advice about the next action."""
    cause = verify_inconclusive_cause(REASON_VERIFY_INCONCLUSIVE, reflection_measured)
    return f"The check was inconclusive — {cause}."


def verify_inconclusive_message(reflection_measured: bool | None) -> str:
    """``REASON_VERIFY_INCONCLUSIVE``'s household sentence. Single writer.

    The registry entry below holds this function's ``None`` (cause-unknown)
    rendering; the envelope re-renders it with the persisted fact.
    """
    return f"{verify_inconclusive_diagnosis(reflection_measured)} Re-verify to try again."


def locate_failed_diagnosis(pilot_heard: bool | None) -> str:
    """What the locator established, without advice about the next action."""
    if pilot_heard:
        return (
            "JTS could hear the speaker, but couldn't line up the test tones "
            "in the recording."
        )
    return "Couldn't hear the speaker clearly."


def locate_failed_message(pilot_heard: bool | None) -> str:
    """``REASON_LOCATE_FAILED``'s household sentence. Single writer.

    SELECTION, never composition — the shape
    :func:`verify_inconclusive_message` uses, for the same reason: one code,
    two honest causes, and a registry that cannot hold one literal true of
    both. The copy names the operation that failed and asserts no cause.

    ``pilot_heard`` is the discriminator:

    * ``True`` — the pilot pair was measurably heard, so "couldn't hear the
      speaker" is refuted BY THIS CAPTURE. Report the lining-up failure and
      ask for one retry.
    * ``False`` / ``None`` — the pilot failed too, or there is no pilot
      evidence, so the level/microphone reading is supported or unknown. The
      registry holds this rendering.

    Keyed on the EVIDENCE, not on which gate fired: the three call sites
    (:func:`_stimulus_locate_ok`, :func:`_sweep_locate_confidence_ok`, VERIFY's
    ``summed_sweep_heard``) are all locate-confidence floors reading the same
    field, so keying on the site would give one situation two sentences.
    """
    diagnosis = locate_failed_diagnosis(pilot_heard)
    if pilot_heard:
        return f"{diagnosis} Try again."
    return f"{diagnosis} Check the volume and the microphone, then try again."


@dataclass(frozen=True)
class RetryableReasonCopy:
    """One retryable reason's diagnosis and still-available action.

    ``diagnosis`` is the observation that remains true after the slot's last
    extra attempt; ``retry_action`` is appended only where an attempt is still
    available. ``strip_before_join`` supports the em-dash sentences, whose
    standalone diagnosis ends with a period the retryable rendering removes.
    """

    diagnosis: str
    retry_action: str
    joiner: str = " "
    strip_before_join: str = ""

    @property
    def message(self) -> str:
        diagnosis = self.diagnosis
        if self.strip_before_join and diagnosis.endswith(self.strip_before_join):
            diagnosis = diagnosis[: -len(self.strip_before_join)]
        return f"{diagnosis}{self.joiner}{self.retry_action}"


@dataclass(frozen=True)
class ReasonSpec:
    """One terminal verdict's template + budget + copy (§5.10)."""

    code: str
    template: str
    # RETRIABLE-OR-NOT: the COUNT lives in
    # :data:`MAX_EXTRA_ATTEMPTS_PER_POSITION`. Zero means "no extra attempt can
    # help" — a statement about the CONDITION, not a budget — and those codes
    # stop the moment they fire. Any non-zero value says only "retriable"; the
    # specific 1 vs 2 does not change behaviour. See
    # :data:`NON_RETRIABLE_CODES`.
    retry_budget: int
    # Short banner shown while a transient code auto-retries (template 1). Empty
    # for codes whose template is a decision screen.
    banner: str
    # The fix/action copy the decision-screen template renders. One reason, one
    # action (the Language guide).
    message: str
    # Optional per-reason override for the HARD-STOP screen's action button.
    # Consulted by that template ONLY: it is the one screen whose default
    # action is a generic destination (``/sound/``) rather than a load-bearing
    # control. Shape is the ``next_action`` mapping the envelope emits:
    # ``{"id", "label", "href"}``.
    next_action: Mapping[str, Any] | None = None
    # Structured only for retryable rows. ``message``/``banner`` above is
    # derived from this value by :func:`_retriable_reason`, so the diagnosis
    # used at exhaustion and the one inside retry copy have one writer.
    retry_copy: RetryableReasonCopy | None = None


def _retriable_reason(
    code: str,
    template: str,
    retry_budget: int,
    copy: RetryableReasonCopy,
    *,
    auto_retry: bool = False,
) -> ReasonSpec:
    """Build a retryable registry row from one structured copy source."""
    if retry_budget <= 0:
        raise ValueError("a retryable reason needs a positive retry budget")
    return ReasonSpec(
        code,
        template,
        retry_budget,
        copy.message if auto_retry else "",
        "" if auto_retry else copy.message,
        retry_copy=copy,
    )


# The §5.10 table, as data. The envelope and the session both read it, so copy
# and budget never drift between the verdict and its screen.
REASON_REGISTRY: dict[str, ReasonSpec] = {
    REASON_AGC_BEHAVIORAL_FAIL: _retriable_reason(
        REASON_AGC_BEHAVIORAL_FAIL, TEMPLATE_FIX_AND_RETRY, 1,
        # The captured two-pilot level delta did not match the programmed one
        # at a level where it should have. Two things produce that — the input
        # chain riding gain, or the speaker's own output compressing — so the
        # copy names the observation, not a cause. The definite mic accusation
        # lives ONLY on REASON_VERIFY_LEVEL_SHIFT.
        RetryableReasonCopy(
            "The two test tones didn't come back at the levels JTS played them.",
            "Re-allow the microphone, then try again.",
        ),
    ),
    REASON_NOISY_ROOM_LINEARITY: _retriable_reason(
        REASON_NOISY_ROOM_LINEARITY, TEMPLATE_FIX_AND_RETRY, 1,
        RetryableReasonCopy(
            "The room got loud during that measurement.",
            "quiet it and try again.",
            joiner=" — ",
            strip_before_join=".",
        ),
    ),
    REASON_PILOT_LEVEL_COLLAPSE: _retriable_reason(
        REASON_PILOT_LEVEL_COLLAPSE, TEMPLATE_FIX_AND_RETRY, 1,
        # The cause is genuinely two-sided and naming only half of it would be
        # the over-claim this code exists to stop.
        RetryableReasonCopy(
            "The test tones didn't rise clearly above the room — it was too "
            "loud, or the speaker too quiet, for this check.",
            "Quiet the room or move the microphone closer, then try again.",
        ),
    ),
    REASON_SNR_FLOOR: _retriable_reason(
        REASON_SNR_FLOOR, TEMPLATE_FIX_AND_RETRY, 1,
        RetryableReasonCopy(
            "The room is too loud right now, or the microphone is too far away.",
            "Quiet the room or move the microphone closer, then try again.",
        ),
    ),
    REASON_CHANNEL_MAP_MISMATCH: ReasonSpec(
        REASON_CHANNEL_MAP_MISMATCH, TEMPLATE_HARD_STOP, 0, "",
        # The numbers behind the refusal are on
        # `event=correction.crossover_v2_check_diag`, which publishes each
        # role's raw rises, isolation ratio, and bound.
        "JTS could not confirm that the drivers played in the expected order. "
        "Return to speaker setup and check the wiring before measuring again.",
    ),
    REASON_ANCHOR_AMBIGUOUS: _retriable_reason(
        REASON_ANCHOR_AMBIGUOUS, TEMPLATE_FIX_AND_RETRY, 1,
        # About the RECORDING and not the speaker: this code fires when the
        # evidence does not identify which driver played what, so naming a
        # cause in the speaker would be an over-claim. Re-recording clears it,
        # because the anchor collapse is a property of one take.
        RetryableReasonCopy(
            "JTS couldn't line that recording up with the test tones it played.",
            "Try that measurement again.",
        ),
    ),
    REASON_CLIPPED: _retriable_reason(
        REASON_CLIPPED, TEMPLATE_SILENT_AUTO_RETRY, 1,
        RetryableReasonCopy(
            "That was a touch loud.",
            "measuring again a bit quieter.",
            joiner=" — ",
            strip_before_join=".",
        ),
        auto_retry=True,
    ),
    REASON_DRIFT_BASELINES_DISAGREE: _retriable_reason(
        REASON_DRIFT_BASELINES_DISAGREE, TEMPLATE_SILENT_AUTO_RETRY, 1,
        RetryableReasonCopy(
            "The capture glitched.",
            "measuring again.",
            joiner=" — ",
            strip_before_join=".",
        ),
        auto_retry=True,
    ),
    REASON_DELAY_EXCEEDS_SEARCH_WINDOW: _retriable_reason(
        REASON_DELAY_EXCEEDS_SEARCH_WINDOW, TEMPLATE_FIX_AND_RETRY, 1,
        RetryableReasonCopy(
            "The microphone may be off the spot in the picture.",
            "Re-check its placement, then try again.",
        ),
    ),
    REASON_LOCATE_FAILED: _retriable_reason(
        REASON_LOCATE_FAILED, TEMPLATE_FIX_AND_RETRY, 1,
        # NOT a literal: the sentence's one writer is
        # ``locate_failed_message``, and what the registry holds is its
        # no-pilot-evidence rendering, true for any reader with no capture in
        # hand. The capture verdict and the envelope re-render it with the
        # measured fact.
        RetryableReasonCopy(
            locate_failed_diagnosis(None),
            "Check the volume and the microphone, then try again.",
        ),
    ),
    REASON_CAPTURE_TIMEOUT: ReasonSpec(
        REASON_CAPTURE_TIMEOUT, TEMPLATE_SESSION_RESTART, 0, "",
        # The old link is dead once the session collapses, so the copy must not
        # say "open the link again" — that link and its QR are gone. Start
        # over mints a FRESH session from this page.
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
    REASON_MEASUREMENT_VOLUME_DRIFT: ReasonSpec(
        REASON_MEASUREMENT_VOLUME_DRIFT, TEMPLATE_HARD_STOP, 0, "",
        # NAMES THE OBSERVATION, NOT A CAUSE. Two conditions reach this code:
        # the fader was read and would not hold (something else owns the
        # volume), or it could not be read at all (the DSP is not answering).
        # Which one fired is on the
        # ``event=active_speaker.measurement_fader_drift result=refused`` line:
        # an empty ``observed_db`` is the unreadable case.
        "JTS could not confirm the speaker was at the level it set for "
        "measuring, so it stopped rather than record a measurement it cannot "
        "trust. Try measuring again; if it keeps happening, restart the "
        "speaker from the system page.",
    ),
    REASON_PROTECTION_SWEEP_TOO_LOW: ReasonSpec(
        REASON_PROTECTION_SWEEP_TOO_LOW, TEMPLATE_HARD_STOP, 0, "",
        "JTS played the measurement fine, but it swept this driver lower than "
        "the driver's own protection lets through, so the bottom of the sweep "
        "is too quiet to trust. Re-check this driver's protection settings in "
        "speaker setup, then measure again.",
    ),
    REASON_PROTECTION_NOT_SEPARABLE: ReasonSpec(
        REASON_PROTECTION_NOT_SEPARABLE, TEMPLATE_HARD_STOP, 0, "",
        "JTS played the measurement fine, but the safety limits it had to keep "
        "in place overlap the crossover you have set, so it cannot tell the two "
        "apart well enough to trust the result. Change the crossover frequency "
        "in speaker setup, then measure again.",
    ),
    REASON_PROGRAM_PROFILE_NOT_CONFIRMED: ReasonSpec(
        REASON_PROGRAM_PROFILE_NOT_CONFIRMED, TEMPLATE_HARD_STOP, 0, "",
        # The states that reach here are ``stale`` (the outputs moved
        # underneath the saved limits) and ``malformed`` (JTS cannot read them
        # back). Both end the same way: open the limits and save them again.
        # There is no separate confirm step — saving the declaration IS
        # declaring it — so the copy does not name one.
        "JTS could not use this speaker's saved safety limits, so it did not "
        "play the measurement signal. Review the limits in speaker setup and "
        "save them again, then measure.",
        next_action={
            "id": "review_safety_limits",
            "label": "Review safety limits",
            # ``/sound/``'s Component setup card renders the hoisted review
            # callout under this exact id whenever the limits are unusable
            # (deploy/assets/sound-profile/js/main.js), and its boot path opens
            # the owning step for this fragment.
            "href": "/sound/setup/#confirm-safety-limits",
        },
    ),
    REASON_MEASUREMENT_TARGETS_MISSING: ReasonSpec(
        REASON_MEASUREMENT_TARGETS_MISSING, TEMPLATE_HARD_STOP, 0, "",
        "JTS does not have a measurement target for every driver this speaker "
        "declares, so it cannot measure them. Finish speaker setup so each "
        "driver is assigned to an output, then measure again.",
        next_action={
            "id": "speaker_setup",
            "label": "Finish speaker setup",
            "href": "/sound/setup/",
        },
    ),
    REASON_SPEAKER_SHAPE_UNSUPPORTED: ReasonSpec(
        REASON_SPEAKER_SHAPE_UNSUPPORTED, TEMPLATE_HARD_STOP, 0, "",
        "JTS can measure a single full-range speaker or a two-way active "
        "crossover, and this speaker is neither. There is nothing to retry — "
        "check the drivers declared in speaker setup.",
        next_action={
            "id": "speaker_setup",
            "label": "Open speaker setup",
            "href": "/sound/setup/",
        },
    ),
    REASON_PROGRAM_PROFILE_MISSING: ReasonSpec(
        REASON_PROGRAM_PROFILE_MISSING, TEMPLATE_HARD_STOP, 0, "",
        # NOT "review the safety limits": there are none to review and no
        # callout naming them.
        "This speaker's driver details are not finished, so JTS has no safety "
        "limits to measure within. Finish the driver details in speaker setup, "
        "then measure again.",
        next_action={
            "id": "speaker_setup",
            "label": "Finish speaker setup",
            # No fragment: ``/sound/`` renders no review callout in this state,
            # so a deep link would land on nothing.
            "href": "/sound/setup/",
        },
    ),
    REASON_PROGRAM_PROFILE_INCOMPLETE: ReasonSpec(
        REASON_PROGRAM_PROFILE_INCOMPLETE, TEMPLATE_HARD_STOP, 0, "",
        # Matches what ``/sound/``'s own callout says in this state: the action
        # is adding the values, not saving — a save with values missing
        # rebuilds an ``incomplete`` profile.
        "Some of this speaker's safety limits are still missing, so JTS did "
        "not play the measurement signal. Add them under Advanced in speaker "
        "setup, then save and measure again.",
        next_action={
            "id": "add_safety_limits",
            "label": "Add the missing limits",
            # The callout DOES render for this state, naming the
            # add-the-values action, so the fragment lands on the explanation.
            "href": "/sound/setup/#confirm-safety-limits",
        },
    ),
    REASON_INTERNAL_ERROR: ReasonSpec(
        REASON_INTERNAL_ERROR, TEMPLATE_FIX_AND_RETRY, 0, "",
        "Something went wrong on the speaker during that measurement. "
        "Try again.",
    ),
    REASON_VERIFY_OUT_OF_TOLERANCE: _retriable_reason(
        REASON_VERIFY_OUT_OF_TOLERANCE, TEMPLATE_VERIFY_FAIL, 2,
        RetryableReasonCopy(
            "The result didn't quite match the prediction.",
            "Try again.",
        ),
    ),
    # Budget 0 — the ONE verify_fail row that is not retriable: a second
    # graded attempt already agreed with the first inside the instrument's
    # repeat floor, so a third lands in the same place. For a non-retriable
    # code ``_verify_fail_envelope`` promotes Re-measure to the primary rather
    # than offering a "Try again" this row has ruled out.
    REASON_VERIFY_DETERMINISTIC_MISMATCH: ReasonSpec(
        REASON_VERIFY_DETERMINISTIC_MISMATCH, TEMPLATE_VERIFY_FAIL, 0, "",
        "JTS checked twice and measured the same difference both times, so "
        "this is what your speaker actually does — not a bad measurement, and "
        "another try lands in the same place. Re-measure to fit the crossover "
        "again.",
    ),
    REASON_VERIFY_CROSSOVER_REGION: _retriable_reason(
        REASON_VERIFY_CROSSOVER_REGION, TEMPLATE_VERIFY_FAIL, 2,
        # Says what was measured, no diagnosis — a handoff dip can be
        # alignment, spacing, Fc, or the horn, and this cannot tell them apart.
        # The hint does not lead with "try again": a retry re-checks the SAME
        # applied graph and this defect is deterministic.
        RetryableReasonCopy(
            "The two drivers didn't blend as designed where they hand over.",
            "Re-measure to fit it again.",
        ),
    ),
    REASON_VERIFY_INCONCLUSIVE: _retriable_reason(
        REASON_VERIFY_INCONCLUSIVE, TEMPLATE_VERIFY_FAIL, 2,
        # NOT a literal: the sentence's one writer is
        # ``verify_inconclusive_message``, and what the registry holds is its
        # cause-unknown rendering, true for any reader with no gate record.
        # The envelope re-renders it with the persisted fact.
        RetryableReasonCopy(
            verify_inconclusive_diagnosis(None),
            "Re-verify to try again.",
        ),
    ),
    REASON_VERIFY_LEVEL_SHIFT: _retriable_reason(
        REASON_VERIFY_LEVEL_SHIFT, TEMPLATE_VERIFY_FAIL, 2,
        # The instrument is named device-agnostically: the session mic may be a
        # UMIK-2 or a laptop. ONE string renders on TWO surfaces where "try
        # again" is a DIFFERENT control — the measurement page's in-session
        # re-arm, which re-compares against the SAME reference and repeats
        # until the budget dies, and the wizard's FRESH capture session, which
        # re-baselines and settles in one capture — so it names the escalation
        # conditionally rather than commanding or dismissing the retry.
        RetryableReasonCopy(
            "The microphone's levels changed between measurements, so this "
            "check couldn't settle.",
            "Try again — if it repeats, re-measure.",
        ),
    ),
    REASON_DELAY_IMPLAUSIBLE: _retriable_reason(
        REASON_DELAY_IMPLAUSIBLE, TEMPLATE_FIX_AND_RETRY, 1,
        # Names what was measured and no cause (ADR-0002 corollary 2): a lag
        # outside the search window is consistent with the locator latching
        # wrong, with something moving mid-sweep, and with a mispositioned
        # microphone, and this capture separated none of them.
        RetryableReasonCopy(
            "The delay JTS measured between the drivers isn't one this "
            "speaker's geometry can produce.",
            "Measure again — if it repeats, check that nothing moved during "
            "the sweep.",
        ),
    ),
    REASON_APPLY_FAILED: _retriable_reason(
        REASON_APPLY_FAILED, TEMPLATE_FIX_AND_RETRY, 1,
        RetryableReasonCopy(
            "JTS could not apply the measured crossover automatically.",
            "Try again.",
        ),
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
    REASON_POSITION_HOLD_EXPIRED: ReasonSpec(
        REASON_POSITION_HOLD_EXPIRED, TEMPLATE_SESSION_RESTART, 0, "",
        "Nothing reported the microphone reaching its next position, so the "
        "measurement stopped waiting. Start over from this page when every "
        "position can be confirmed as the microphone arrives.",
    ),
    REASON_POSITION_TARGET_MISSING: ReasonSpec(
        REASON_POSITION_TARGET_MISSING, TEMPLATE_SESSION_RESTART, 0, "",
        "This measurement did not say where the microphone should be, so it "
        "stopped rather than record an unknown position. Start over from this "
        "page.",
    ),
    REASON_SESSION_CEILING_EXPIRED: ReasonSpec(
        REASON_SESSION_CEILING_EXPIRED, TEMPLATE_SESSION_RESTART, 0, "",
        "The whole measurement ran out of time while it was still waiting for "
        "the microphone to reach a position. Start over from this page once "
        "the microphone can be moved through the walk more quickly.",
    ),
    REASON_GEOMETRY_RETAKE_UNREACHABLE: ReasonSpec(
        REASON_GEOMETRY_RETAKE_UNREACHABLE, TEMPLATE_SESSION_RESTART, 0, "",
        "The room needs the microphone measured from a wider spot, and from "
        "above the mark, than this measurement can ask for. Run a Full "
        "measurement that prompts each spot on screen, and walk those spots by "
        "hand, to finish tuning this speaker.",
    ),
    REASON_CLOUD_GEOMETRY_LOCKED: _retriable_reason(
        REASON_CLOUD_GEOMETRY_LOCKED, TEMPLATE_FIX_AND_RETRY,
        # RETRIABLE (any non-zero value; see ``ReasonSpec.retry_budget``). The
        # count is the session's own ceiling on wider-spot asks —
        # ``_close_cloud_group`` stops at ``GEOMETRY_RETRY_POSITIONS`` — not
        # what admits the retake: every rung spends one of the POSITION's
        # pooled extras.
        GEOMETRY_RETRY_POSITIONS,
        # Copy names the ACTION, not the diagnosis — a household has no way to
        # judge "the echo estimates clustered". The per-attempt wider-spot
        # instruction rides the verdict payload's ``prompt`` field on top of
        # this (see ``_cloud_measure_group_verdict``).
        RetryableReasonCopy(
            "These spots were too close together to tell a real dip from an echo.",
            "Take this one from further out and we will use it instead.",
        ),
    ),
    # The delta-probe rollbacks: all three TEMPLATE_HARD_STOP with no retry
    # budget, because the correction has already been undone and "try again"
    # would re-run the same measurement into the same defect. Each names what
    # was restored FIRST, then the one thing that would change the outcome. No
    # hardware nouns, matching the null-classification copy rule.
    REASON_CORRECTION_MODEL_ERROR: ReasonSpec(
        REASON_CORRECTION_MODEL_ERROR, TEMPLATE_HARD_STOP, 0, "",
        "JTS checked the tuning against what your speaker actually did, and "
        "they did not match — so the previous sound has been put back. This "
        "usually means something in the chain is not behaving as described; "
        "re-check the driver details in speaker setup, then measure again.",
    ),
    REASON_CORRECTION_LEVEL_SHORTFALL: ReasonSpec(
        REASON_CORRECTION_LEVEL_SHORTFALL, TEMPLATE_HARD_STOP, 0, "",
        "Your speaker delivered noticeably less than the tuning asked it for, "
        "so the previous sound has been put back. Try measuring again at a "
        "lower listening volume.",
    ),
    REASON_CORRECTION_SPATIALLY_COSTLY: ReasonSpec(
        REASON_CORRECTION_SPATIALLY_COSTLY, TEMPLATE_HARD_STOP, 0, "",
        "The tuning helped at the measuring spot but made the sound less even "
        "elsewhere in the room, so the previous sound has been put back. "
        "Moving the speaker away from nearby walls and surfaces, then "
        "measuring again, is what changes this.",
    ),
    # Renders only when the restore actually ran; the failed-restore row below
    # is what renders when it did not. The remedy differs from its neighbours
    # because the finding does: nothing misbehaved, so there is no chain to
    # re-check and no level to drop.
    REASON_CORRECTION_MEASURED_REGRESSION: ReasonSpec(
        REASON_CORRECTION_MEASURED_REGRESSION, TEMPLATE_HARD_STOP, 0, "",
        "JTS measured your speaker before and after the tuning, and it "
        "sat further from flat afterwards — so the previous sound has been put back. "
        "Nothing is broken; this room and this speaker position did not suit "
        "the tuning. Moving the speaker a little, or measuring from your usual "
        "listening spot, is what changes this.",
    ),
    # The one row here that reports a NON-finding: it says what could not be
    # established before what was done about it.
    REASON_CORRECTION_UNPROVEN_BOOST: ReasonSpec(
        REASON_CORRECTION_UNPROVEN_BOOST, TEMPLATE_HARD_STOP, 0, "",
        "JTS could not measure whether this tuning improved your speaker, and "
        "it turns some parts up rather than only down — so the previous sound "
        "has been put back rather than leaving an unproven change driving your "
        "speaker harder. Measuring again, from your usual listening spot, is "
        "what settles it.",
    ),
    REASON_CORRECTION_UNSAFE_RESULT: ReasonSpec(
        REASON_CORRECTION_UNSAFE_RESULT, TEMPLATE_HARD_STOP, 0, "",
        "JTS checked what your speaker actually did with this tuning and "
        "measured more output than the tuning declared, so the previous sound "
        "has been put back rather than leaving it playing. Measuring again, "
        "from your usual listening spot, is what settles it.",
    ),
    REASON_CORRECTION_UNVERIFIABLE_RESULT: ReasonSpec(
        REASON_CORRECTION_UNVERIFIABLE_RESULT, TEMPLATE_HARD_STOP, 0, "",
        "JTS could not complete the check that confirms a new tuning, so the "
        "previous sound has been put back rather than leaving a change nobody "
        "has measured on your speaker. Measuring again, from your usual "
        "listening spot, is what settles it.",
    ),
    # The five rows above all promise "the previous sound has been put back",
    # which is only true when the rollback actually ran. When it did not, THIS
    # row renders instead — same finding, opposite state of the speaker.
    #
    # ONE row rather than three verdict-specific ones: the route out is the
    # same in all three cases, and the specific finding is on the verdict
    # itself (``delta_probe.verdict``, in the payload and the journal).
    REASON_CORRECTION_ROLLBACK_FAILED: ReasonSpec(
        REASON_CORRECTION_ROLLBACK_FAILED, TEMPLATE_HARD_STOP, 0, "",
        "JTS checked the tuning against what your speaker actually did, and "
        "they did not match — but it could not put the previous sound back on "
        "its own, so the new tuning is STILL APPLIED. Go back to the previous "
        "tuning, or measure again.",
    ),
}

# The transient codes whose first retry is automatic (a banner, no decision
# screen) per §5.10 template 1.
TRANSIENT_AUTO_RETRY_CODES = frozenset(
    code for code, spec in REASON_REGISTRY.items()
    if spec.template == TEMPLATE_SILENT_AUTO_RETRY
)

#: The capture-consuming ladders' refusal KINDS, mapped to the codes whose
#: copy the household reads. A mapping rather than an identity because two
#: kinds do NOT share their code's name: a glitched timeline renders as
#: ``drift_baselines_disagree`` and a bent curve as ``agc_behavioral_fail``.
#: Completeness is checked — see :func:`_screen_refusal_code` and
#: ``test_every_screen_kind_has_a_household_sentence``.
SCREEN_KIND_REASONS: dict[str, str] = {
    _spatial.SCREEN_LOCATE_FAILED: REASON_LOCATE_FAILED,
    _spatial.SCREEN_PILOT_LEVEL_COLLAPSE: REASON_PILOT_LEVEL_COLLAPSE,
    _spatial.SCREEN_LINEARITY_FAILED: REASON_AGC_BEHAVIORAL_FAIL,
    _spatial.SCREEN_CAPTURE_GLITCH: REASON_DRIFT_BASELINES_DISAGREE,
    _spatial.SCREEN_CLIPPED: REASON_CLIPPED,
    # The six an ANCHOR phase adds. Two do not share their code's name
    # either: an unresolved alignment renders as
    # ``delay_exceeds_search_window``, and a bent curve the room caused renders
    # as ``noisy_room_linearity``.
    _dispatch.SCREEN_ANCHOR_AMBIGUOUS: REASON_ANCHOR_AMBIGUOUS,
    _dispatch.SCREEN_CHANNEL_MAP_MISMATCH: REASON_CHANNEL_MAP_MISMATCH,
    _dispatch.SCREEN_SNR_FLOOR: REASON_SNR_FLOOR,
    _dispatch.SCREEN_NOISY_ROOM_LINEARITY: REASON_NOISY_ROOM_LINEARITY,
    _dispatch.SCREEN_ALIGNMENT_UNRESOLVED: REASON_DELAY_EXCEEDS_SEARCH_WINDOW,
    _dispatch.SCREEN_DELAY_IMPLAUSIBLE: REASON_DELAY_IMPLAUSIBLE,
}


def _screen_refusal_code(kind: str) -> str:
    """One screen kind's household code, LOUDLY on an unmapped one.

    An unmapped kind is a wiring defect — a ladder step shipped without a
    sentence. It still returns rather than raising, under the most conservative
    code available: losing the refusal to a mapping gap would be worse than
    naming it imprecisely.
    """
    code = SCREEN_KIND_REASONS.get(kind)
    if code is not None:
        return code
    log_event(
        logger, "correction.crossover_v2_screen_kind_unmapped",
        level=logging.ERROR, kind=str(kind),
    )
    return REASON_LOCATE_FAILED


def correction_rollback_failed_message(rollback_anchor_available: bool | None) -> str:
    """``correction_rollback_failed``'s sentence, branched on the anchor.

    * ``True``/``None`` — a restore was attempted and did not complete: there
      IS a stored previous sound, so going back to it is a real remedy.
    * ``False`` — no stored previous sound; this arm names the two levers that
      remain instead of a way back that does not exist.

    The ``False`` arm states no CAUSE. What it reports is "no prior candidate
    fingerprint is recorded", which is every first-ever apply but also any
    prior profile that was not a measured-candidate apply.

    ``None`` takes the way-back arm: an unestablished fact must not invent the
    more alarming claim about a speaker that may have a good anchor.
    """
    if rollback_anchor_available is False:
        return (
            "The new tuning is still applied, and JTS has no previous sound it "
            "can safely put back on this speaker. You can measure again to try "
            "for a better result, or clear the tuning from the Sound page to "
            "return to the standard setup."
        )
    return (
        "JTS checked the tuning against what your speaker actually did, and "
        "they did not match — but it could not put the previous sound back, "
        "so the newer tuning is STILL APPLIED. Go back to the previous "
        "tuning, or measure again."
    )


def reason_message(
    code: str,
    spec: ReasonSpec,
    *,
    pilot_heard: bool | None = None,
    reflection_measured: bool | None = None,
    rollback_anchor_available: bool | None = None,
) -> str:
    """The household sentence for ``code``, given what the capture measured.

    THE single copy selector: one failure is narrated on surfaces that never
    see each other — the capture verdict (:meth:`PhaseVerdict.to_capture_dict`),
    the envelope (``crossover_envelope_v2._reason_message``), and the
    apply-seam refusal — and a household looking at two of them after ONE
    failure must not be handed two accounts of it. Adding a third
    evidence-keyed code means adding a branch HERE; a caller that renders
    ``spec.message`` directly re-opens the gap.

    Exhaustion is state-aware: :meth:`authorize_begin` keeps the diagnosis
    selected here but replaces retry advice with the terminal outcome. The
    observation must agree across surfaces; an action that is no longer
    available must not survive.

    ``spec`` is passed in rather than looked up so each caller keeps its own
    existence guard — ``REASON_REGISTRY[code]`` raising ``KeyError`` on an
    unregistered code is load-bearing in :meth:`_refuse`.

    Facts are keyword-only and default to "not established", so a caller
    holding none of them gets the registry's own renderings.
    """
    if code == REASON_LOCATE_FAILED:
        message = locate_failed_message(pilot_heard)
    elif code == REASON_VERIFY_INCONCLUSIVE:
        message = verify_inconclusive_message(reflection_measured)
    elif code == REASON_CORRECTION_ROLLBACK_FAILED:
        # Answered on the round's RECORDED anchor: re-deciding it here on a
        # live fact would give one failure two accounts.
        message = correction_rollback_failed_message(rollback_anchor_available)
    else:
        # ``or spec.banner`` for the silent-auto-retry codes, whose household
        # text IS the banner and whose ``message`` is empty by construction.
        message = spec.message or spec.banner
    return message


def reason_diagnosis(
    code: str,
    spec: ReasonSpec,
    *,
    pilot_heard: bool | None = None,
    reflection_measured: bool | None = None,
) -> str:
    """The observation inside any retryable reason, without retry advice.

    The two evidence-keyed reasons select their diagnosis from this capture's
    facts; every literal reason reads the diagnosis stored in its
    :class:`RetryableReasonCopy`, which also composes the registry's full
    retryable ``message``/``banner``.
    """
    if code == REASON_LOCATE_FAILED:
        return locate_failed_diagnosis(pilot_heard)
    if code == REASON_VERIFY_INCONCLUSIVE:
        return verify_inconclusive_diagnosis(reflection_measured)
    return spec.retry_copy.diagnosis if spec.retry_copy is not None else ""


# Conditions no extra attempt can clear. A rejection carrying one rides out as
# a TERMINAL capture verdict at the settle
# (:data:`~.admission.SETTLE_CONDITION_NOT_RETRIABLE`), so the phone renders
# its terminal screen instead of a "Try again" the next begin would refuse.
# ``assess_begin``'s ``REFUSE_NON_RETRIABLE`` is the BACKSTOP for a begin that
# reaches a settled slot anyway, not the ordinary path.
NON_RETRIABLE_CODES = frozenset(
    code for code, spec in REASON_REGISTRY.items() if spec.retry_budget == 0
)


@dataclass(frozen=True)
class PhaseVerdict:
    """A consume verdict: the capture dict + the internal reason (if any)."""

    accepted: bool
    code: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    # Whether THIS capture's leading pilot pair cleared the room's own in-band
    # floor — ``analysis.pilot_snr_ok``, carried verbatim including its
    # ``None`` (no pilot evidence). The fact ``locate_failed``'s copy branches
    # on. Carried on the verdict rather than dug out of ``payload`` because it
    # is decided at the gate, where the analysis is in hand.
    pilot_heard: bool | None = None
    # VERIFY's gate discriminator for ``verify_inconclusive``, on the verdict
    # for the same reason: terminal exhaustion must repeat this capture's
    # diagnosis, not the registry's evidence-unknown fallback.
    reflection_measured: bool | None = None

    def to_capture_dict(self) -> dict[str, Any]:
        """The mapping ``consume_capture`` returns to ``run_capture_plan``.

        Always carries ``accepted``; a rejection adds the reason code,
        template and copy so the phone renders the right §5.10 screen. Every
        non-``accepted`` field is relayed verbatim in the ``capture_result``
        host event.

        ``reason`` comes from :func:`reason_message` rather than the registry
        entry, so a code whose honest sentence depends on what was measured
        renders that sentence HERE and not only in the envelope served later.
        """
        out: dict[str, Any] = {"accepted": self.accepted}
        if self.code is not None:
            spec = REASON_REGISTRY[self.code]
            out.update(
                code=self.code,
                template=spec.template,
                reason=reason_message(
                    self.code,
                    spec,
                    pilot_heard=self.pilot_heard,
                    reflection_measured=self.reflection_measured,
                ),
                banner=spec.banner,
                auto_retry=self.code in TRANSIENT_AUTO_RETRY_CODES,
                pilot_heard=self.pilot_heard,
            )
            if self.code == REASON_VERIFY_INCONCLUSIVE:
                out["reflection_measured"] = self.reflection_measured
        out.update(self.payload)
        return out
