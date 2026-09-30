# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Refusal codes, templates, retry budgets and operator copy."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Mapping, Sequence

from jasper.audio_measurement import evidence_reasons
from jasper.audio_measurement.ramp import SPL_CEILING_EXCEEDED
from jasper.audio_measurement.frame_ledger import LOST_AT_CAPTURE_OVERRUN
from jasper.audio_measurement.measurement_geometry import DECLARED_GEOMETRY_UNREADABLE
from jasper.audio_measurement.wired_capture import CODE_CAPTURE_GAIN_UNVERIFIED
from jasper.platform.speaker_layout import MAIN_DRIVER_ROLES_BY_MODE, measurement_target_name, measurement_target_parts

from .spatial import GEOMETRY_RETRY_POSITIONS

logger = logging.getLogger(__name__)

LOCATE_RETRY_ACTION = "Check the volume and the microphone, then try again."
TIMING_RESET_NOTE = (
    "Timing is the physical arrival difference between the drivers. Once measured with confidence it does not change "
    "with EQ, room or bass work. Reset it only if you moved or replaced a driver, changed the enclosure, or changed "
    "the crossover so much that you want a fresh read."
)


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
# loud ambient burst rather than the phone's AGC. ``capture_dispatch.assess``
# distinguishes the two on the CHECK gain solve's own ``gain_plan.
# snr_floor_ok``, computed against this capture's ambient bands independent of
# the linearity outcome.
REASON_NOISY_ROOM_LINEARITY = "noisy_room_linearity"
# CHECK needs a level solve even when pilot SNR and linearity are unknown.
REASON_PILOT_LEVEL_COLLAPSE = "pilot_level_collapse"
REASON_SNR_FLOOR = "snr_floor"
REASON_CHANNEL_MAP_MISMATCH = "channel_map_mismatch"
# The analyzer could not decide WHICH scheduled tone a capture's first arrival
# was, so it cannot say which driver played what. Retriable, and its copy names
# the recording rather than the speaker: the alternative,
# `REASON_CHANNEL_MAP_MISMATCH`, is a hard stop telling a household to open its
# speaker, and the evidence cannot support that. Ladder rung:
# `capture_dispatch.assess`.
REASON_ANCHOR_AMBIGUOUS = "anchor_ambiguous"
REASON_ANCHOR_TOO_QUIET = "anchor_too_quiet"
# The test tones cleared the room but the sweep after them did not (#5672).
REASON_SWEEP_MISSING = "sweep_missing"
REASON_PILOT_STEP_IMPLAUSIBLE = "pilot_step_implausible"
REASON_CLIPPED = "clipped"
REASON_LEVEL_DRIFT_AT_SESSION_GAIN = "level_drift_at_session_gain"
REASON_LEVEL_OFF_TARGET = "level_off_target"
REASON_DRIFT_BASELINES_DISAGREE = "drift_baselines_disagree"
REASON_CAPTURE_OVERRUN = LOST_AT_CAPTURE_OVERRUN
REASON_DELAY_EXCEEDS_SEARCH_WINDOW = "delay_exceeds_search_window"
REASON_LOCATE_FAILED = "locate_failed"
REASON_VOLUME_UNRESOLVED = "volume_unresolved"
REASON_PROGRAM_UNPLAYABLE = "program_unplayable"
# #2059: a plan-shape request the household's link/client sent that this build
# does not recognize -- an unknown tier, or a position count outside its
# tier's range. Distinct from
# ``program_unplayable`` -- that copy's "re-check the driver details" advice
# is a loose fit for a malformed request, which no driver recheck fixes.
REASON_PROGRAM_PLAN_SHAPE_INVALID = "program_plan_shape_invalid"
# The main fader was not at the volume this session declared when a stimulus
# was about to play, and re-asserting it could not be proven. The program was
# admissible; the SPEAKER's level was not the one it was admitted against.
# Terminal: the re-assert has already been tried and could not be confirmed.
# NOT ``volume_unresolved``, whose subject is the RESTORE path.
REASON_MEASUREMENT_VOLUME_DRIFT = "measurement_volume_drift"
REASON_MEASUREMENT_OUTPUT_MUTED = "measurement_output_muted"
REASON_MEASUREMENT_GRAPH_UNAVAILABLE = "measurement_graph_unavailable"
# The program PLAYED; the offline evidence math refused. §4.2 divides the
# emitted measurement protection back out of the capture, and on a
# candidate-required bin that division is inadmissible when the protection
# attenuates more than 12 dB or the recovery would exceed 12 dB. Deterministic,
# so terminal. The offending slug rides out in the refusal detail.
REASON_PROTECTION_NOT_SEPARABLE = "protection_not_separable"
# Sibling for the OTHER conditioning branch: `abs(P) < floor` does not involve
# `C`, so "change the crossover frequency" cannot clear it.
REASON_PROTECTION_SWEEP_TOO_LOW = "protection_sweep_too_low"
REASON_PROGRAM_MEASUREMENT_INPUTS_INVALID = "program_measurement_inputs_invalid"
# The session-open shape gate: the walk handles a 1-way passive main or a
# 2-way, and this speaker is neither. Terminal — no household action clears it.
REASON_SPEAKER_SHAPE_UNSUPPORTED = "speaker_shape_unsupported"
# Its sibling one gate later: the shape is walkable, but live status carries no
# measurement target for every role it declares. The roles reach the journal.
REASON_MEASUREMENT_TARGETS_MISSING = "measurement_targets_missing"
# A tweeter's cap is declared or derived from declared sensitivities (ADR-0382).
REASON_DRIVER_SENSITIVITY_UNDECLARED = "driver_sensitivity_undeclared"

# The wired capture kernel stopped a take because the microphone heard the
# speaker above this session's SPL ceiling
# (``audio_measurement.wired_capture.WiredSplCeilingExceeded``, wrapped as
# ``crossover_v2.program_transaction.StimulusCaptureStopped``). Its own code,
# not ``internal_error``: the household can act on this by lowering the level,
# which is not true of a genuine host fault. Terminal.
REASON_SPL_CEILING_EXCEEDED = SPL_CEILING_EXCEEDED

REASON_MEASUREMENT_BASELINE_UNAVAILABLE = "measurement_baseline_unavailable"
REASON_MEASUREMENT_CANDIDATE_SPEAKER_MISMATCH = "measurement_candidate_speaker_mismatch"
REASON_MEASUREMENT_CANDIDATE_REQUIRED = "measurement_candidate_required"
REASON_MEASUREMENT_PROGRAM_NOT_OFFERED = "measurement_program_not_offered"
REASON_MEASUREMENT_CANDIDATE_INVALID = "measurement_candidate_invalid"
REASON_MEASUREMENT_SCOPE_INVALID = "measurement_scope_invalid"
REASON_MEASUREMENT_FILTERS_INVALID = "measurement_filters_invalid"
REASON_MEASUREMENT_BRANCH_CHANNELS = "measurement_branch_channels"
#: The walk's mover and the session's ADVANCE POLICY disagree (a countdown
#: with no hand moving, or a tap-wait from an arm with none to give). NOT a
#: comparison against the session's GATE.
REASON_WALK_MOVER_MISMATCH = "walk_mover_mismatch"
REASON_WALK_RIG_CLEAR_NOT_ATTESTED = "walk_rig_clear_not_attested"
REASON_WALK_MOVER_UNAVAILABLE = "walk_mover_unavailable"
REASON_ARM_PARK_UNCONFIRMED = "arm_park_unconfirmed"
REASON_WALK_OVER_MOVER_ENVELOPE = "walk_over_mover_envelope"
REASON_WALK_LEVEL_POLICY_INVALID = "walk_level_policy_invalid"
REASON_RUN_LEVEL_PILOTS_UNDER_AMBIENT = "run_level_pilots_under_ambient"
REASON_VOLUME_RESTORE_DEFERRED = "volume_restore_deferred"
REASON_WALK_SCHEMA_VERSION_UNSUPPORTED = "walk_schema_version_unsupported"
REASON_MEASURE_SPL_CALIBRATION_REQUIRED = "measure_spl_calibration_required"
REASON_WALK_COMMISSIONING_STOP_UNSET = "walk_commissioning_stop_unset"
REASON_WALK_STIMULUS_NOT_ACCEPTED = "walk_stimulus_not_accepted"
REASON_WALK_OVER_CAPTURE_CAPACITY = "walk_over_capture_capacity"
REASON_WALK_STOP_NO_LONGER_VALID = "walk_stop_no_longer_valid"
REASON_WALK_TEMPLATE_NOT_ACCEPTED = "walk_template_not_accepted"
REASON_WALK_POLARITY_NOT_ACCEPTED = "walk_polarity_not_accepted"
REASON_WALK_DELAY_NOT_ACCEPTED = "walk_delay_not_accepted"
REASON_WALK_LEVEL_MATCH_NO_EVIDENCE = "walk_level_match_no_evidence"
REASON_WALK_CANDIDATE_NOT_MEASURABLE = "walk_candidate_not_measurable"
REASON_WALK_BRANCH_PAIR_UNDECLARED = "walk_branch_pair_undeclared"
REASON_WALK_LAYOUT_UNSUPPORTED_FOR_PER_DRIVER_PROGRAMS = "walk_layout_unsupported_for_per_driver_programs"
REASON_WALK_NOTHING_PLAYABLE = "walk_nothing_playable"

# Any OTHER host-side fault the session runner's catch-all cleanup arm caught.
# The seams raise open-endedly (CamillaUnavailable is a bare Exception,
# analyze/emit raise ValueError/RuntimeError, the held measurement window
# raises MeasurementWindowError), so an enumerated except list is how failures
# escape with the volume active and the phone frozen. Terminal.
REASON_INTERNAL_ERROR = "internal_error"
# §5.2's "inconclusive — re-verify" verdict: VERIFY's own detected first
# reflection forced a shorter gate than MEASURE's, so the overlay difference is
# not evidence about driver alignment.
REASON_VERIFY_INCONCLUSIVE = "verify_inconclusive"
# A distinct VERIFY outcome: the recording chain drifted between VERIFY
# attempts, not the speaker going out of tolerance.
REASON_VERIFY_LEVEL_SHIFT = "verify_level_shift"
# The applied result tracks the model but does NOT meet the candidate's own
# crossover target through the handoff — a defect present in both the
# measurement and the model cancels out of a measured-vs-model grade.
REASON_VERIFY_CROSSOVER_REGION = "verify_crossover_region"
# The apply transaction came back blocked or raised.
# ``persist_terminal_failure`` scopes its §5.6 evidence reset away from this
# code: an apply failure says nothing about the mic position.
REASON_APPLY_FAILED = "apply_failed"
# A deliberate phone Stop (CaptureAborted, abort_reason == "stopped") is not a
# transport death — see the catch-all's exception classification in
# jasper.web.correction_crossover_v2.
REASON_USER_STOPPED = "user_stopped"
REASON_ARM_HOST_STUCK = "arm_host_stuck"
REASON_RETRIES_SPENT = "retries_spent"
#: A take of a set that levels itself, whose set found no level to play at (ADR-0361 §3, ADR-0403).
REASON_LEVEL_UNSOLVED = "level_unsolved"
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
# The pre-apply cloud closed with its geometry `locked` — every position's
# echo estimate landed on the same tau, so the nulls are not moving and
# spatial averaging cannot fill them. Not a bad capture. The group asks for that position again from a wider spot, at most
# ``GEOMETRY_RETRY_POSITIONS`` times, then proceeds with the verdict recorded
# rather than blocking on a defect no mic move can decorrelate.
REASON_CLOUD_GEOMETRY_LOCKED = "cloud_geometry_locked"
class CrossoverV2Refused(ValueError):
    """A v2 endpoint refusal (maps to HTTP 400 in the dispatch ladder).

    Stamp ``code`` before rendering the envelope: a pre-flight refusal has no
    persisted failure, so its response needs the code to select registry copy
    and an action (see #1821). Unknown provider codes have no registry action.
    """

    def __init__(self, *args: Any, code: str = "", next_action: Mapping[str, Any] | None = None,
                 issues: Iterable[Mapping[str, str]] = ()) -> None:
        super().__init__(*args)
        self.code = code
        self.next_action = next_action
        self.issues = [dict(issue) for issue in issues]


#: A ``channel_map_mismatch`` verdict's evidence names each driver whose pilot failed
#: the check under this prefix (``channel_map_failed.tweeter``).
CHANNEL_MAP_FAILED_PREFIX = "channel_map_failed."


def _low_to_high(target_id: str) -> tuple[int, bool, str]:
    """A driver's place low to high in the speaker layout's role order, whose widest
    mode lists every active role; a primary output before its rear one."""
    role, variant = measurement_target_parts(target_id)
    roles = max(MAIN_DRIVER_ROLES_BY_MODE.values(), key=len)
    return roles.index(role) if role in roles else len(roles), variant != "primary", target_id


def channel_map_failed_roles(*evidence: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(sorted({key.removeprefix(CHANNEL_MAP_FAILED_PREFIX) for each in evidence for key, value in each.items()
                         if key.startswith(CHANNEL_MAP_FAILED_PREFIX) and value is True}, key=_low_to_high))


def channel_map_mismatch_message(failed_roles: Sequence[str]) -> str:
    """``REASON_CHANNEL_MAP_MISMATCH``'s household sentence, naming each driver whose
    pilot failed; the registry holds the rendering that names none (#1922)."""
    names = [f"the {measurement_target_name(role)}" for role in failed_roles]
    if not names:
        fact = "the drivers played in the expected order"
    elif len(names) == 1:
        fact = f"{names[0]} played on its own output"
    else:
        fact = f"{', '.join(names[:-1])} and {names[-1]} played on their own outputs"
    return f"JTS could not confirm that {fact}. Return to speaker setup and check the wiring before measuring again."


def driver_sensitivity_undeclared_message(undeclared: Sequence[str], disagreeing: Sequence[str]) -> str:
    """``REASON_DRIVER_SENSITIVITY_UNDECLARED``'s sentences naming each driver to fix."""
    def named(roles: Sequence[str]) -> str:
        return " and ".join(f"the {role}" for role in roles)

    fixes = [f"Declare the sensitivity of {named(undeclared)} in speaker setup."] if undeclared else []
    if disagreeing:
        fixes.append(f"The outputs of {named(disagreeing)} declare different sensitivities; "
                     "make them agree in speaker setup.")
    return " ".join([*fixes, "Then measure again: JTS sets a tweeter's measurement level from the "
                              "declared driver sensitivities."])


@dataclass(frozen=True)
class RetryableReasonCopy:
    """One retryable reason's copy: what was observed, then the action that may clear it.

    ``strip_before_join`` comes off the diagnosis's end before an em-dash ``joiner``.
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
    # Optional per-reason action: the HARD-STOP screen's button (its default is
    # a generic destination rather than a load-bearing control) and the
    # ``next_action`` of a refusal body or preflight issue (``refusal_copy_for``,
    # ``PreflightIssue.from_code``). Shape is the mapping the envelope emits:
    # ``{"id", "label", "href"}``.
    next_action: Mapping[str, Any] | None = None
    # True only for measured-and-rejected recording quality, never a level or safety fault.
    capture_quality: bool = False


def _retriable_reason(
    code: str,
    template: str,
    retry_budget: int,
    copy: RetryableReasonCopy,
    *,
    auto_retry: bool = False,
    capture_quality: bool = False,
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
        capture_quality=capture_quality,
    )


ARM_STOP_COPY = {
    "power_void": "The turntable reported a power fault. Check its power supply and saved zero before restarting.",
    "move_failed": "The turntable could not move. Check its connection and arm trail; close any other turntable command before restarting.",
    "session_failed": "The measurement session failed. Check the run status before restarting.",
    "idle_ceiling": "The turntable stopped waiting for a position request. Check the run status before restarting.",
    "settle_floor": "The turntable did not get enough time to settle. Check the settle time before restarting.",
    "refused": "The turntable could not use this run setup. Check the mover and run settings before restarting.",
    "release_rejected": "The speaker did not accept the turntable position. Check the run status before restarting.",
    "status_unreachable": "The turntable could not read the speaker status. Check the hostname and connection before restarting.",
    "session_stopped": "The measurement session stopped. Check the run status before restarting.",
    "hangup_parked": "The turntable host lost its terminal connection. Check the connection and arm position before restarting.",
    "terminated_parked": "The turntable host received a termination signal. Check the host and arm position before restarting.",
}
ARM_STOP_REASONS = frozenset(ARM_STOP_COPY) | {REASON_ARM_HOST_STUCK, REASON_INTERNAL_ERROR}

#: The household copy of the analysis-side evidence codes and the command-line tools' own codes, under the next action each names.
_EVIDENCE_COPY: dict[tuple[str, str], dict[str, str]] = {
    ("measure_again", "Measure this round again"): {
        evidence_reasons.REASON_FIT_NOT_FINITE: "A fitted filter term is not a finite number, so the fit is published without numbers.",
        evidence_reasons.REASON_GAP_NOT_CONFIDENT: "The measured arrival gap is below the confidence threshold.",
        evidence_reasons.REASON_GRAPH_MISMATCH: "The summed take played an output the driver-take prediction does not model, "
                                                "so the two sums are not comparable.",
        evidence_reasons.REASON_HARMONIC_WINDOW_OUT_OF_RANGE: "A recording starts too close to a sweep for its harmonic "
                                                              "images to be read.",
        evidence_reasons.REASON_MARK_RESPONSE_UNAVAILABLE: "A mark take's curve cannot be read for the repeat-spread comparison.",
        evidence_reasons.REASON_NO_IMPULSE: "No usable impulse segments are available to measure the arrival gap.",
        evidence_reasons.REASON_SEGMENT_MISSING: "The pair take lacks all three segments on one shared frequency grid.",
        evidence_reasons.REASON_SNR_SHORT: "A driver take is below the alignment signal-to-noise floor, so its predicted sum "
                                           "is not comparable with the measured sum.",
        evidence_reasons.REASON_SWEEP_GRIDS_DISAGREE: "One driver's sweeps in a take were read on different "
                                                      "frequency grids, so they cannot be pooled.",
        evidence_reasons.TAKE_CURVES_NOT_BANKED: "A take in the measurement did not bank a field this view reads.",
        "measurement_captures_missing": "No take in the measurement was captured and analysed.",
        "measurement_capture_identity_mismatch": "A take's recording does not match the identity its record banked.",
        "measurement_program_manifest_missing": "A take's record banked no program manifest.",
        "round_capture_unreadable": "A take's banked record, recording or impulse cannot be read.",
        "round_no_captures": "The round banked no take record this view can read.",
        "round_radiated_band_missing": "A take banked no curve, so the band its driver radiates is unknown.",
        "round_role_not_recorded": "A take banked no impulse for the driver this view reads.",
        "trim_not_finite": "A fitted trim term is not a finite number, so no trim is resolved.",
    },
    ("measure_repeats", "Measure repeat takes at the mark"): {
        evidence_reasons.REASON_FIT_BAND_UNAVAILABLE: "The fit reports no band to compare the mark pairs over.",
        evidence_reasons.REASON_MARK_FIT_BAND_UNAVAILABLE: "A mark take does not cover the fit band above its trusted floor.",
        evidence_reasons.REASON_NO_MARK_PAIRS: "The round has fewer than two takes of this driver at one placement, so no "
                                               "mark pair exists for a repeat spread.",
        evidence_reasons.REASON_NO_REPEATS: "Fewer than two usable repeats are available to measure repeat spread.",
        evidence_reasons.REASON_NO_SHARED_MARK_TAKES: "No driver has mark takes in two of the compared rounds, so nothing "
                                                      "compares between rounds.",
        "unmeasured": "The speaker has no banked repeat floor.",
    },
    ("measure_positions", "Measure more positions"): {
        evidence_reasons.REASON_NO_REFERENCE_TAKE: "The reference take is missing at this position, so no comparison zero exists.",
        evidence_reasons.REASON_NO_ROW: "This position has no measured row.",
        evidence_reasons.REASON_NON_BEARING: "The pose is not a bearing at which the requested figure can be measured.",
        evidence_reasons.REASON_TOO_FEW_POSITIONS: "Too few usable positions support the requested cross-position statistic.",
        "gate_sweep_single_pose": "The gate sweep has one pose, and its spread across poses needs two.",
    },
    ("measure_candidates", "Measure the incumbent and another candidate"): {
        evidence_reasons.REASON_NO_COMPARISON: "One candidate was played, so there is no candidate comparison or repeat "
                                               "spread for it.",
        evidence_reasons.REFUSE_NO_INCUMBENT: "The rear comparison has no usable incumbent set.",
        "candidates_no_ladder": "No pose played two candidates, so the round has no candidate ladder.",
        "no_candidate_takes": "No banked take of the round names a candidate, so the round played none.",
    },
    ("measure_rear", "Measure another rear round"): {
        evidence_reasons.REASON_NO_EARLIER_REFERENCE: "No earlier rear round banked a reference at this position "
                                                      "to compare this round's reference with.",
        evidence_reasons.REASON_REFERENCE_NOT_IN_SET: "This round's reference takes at this position are in none of "
                                                      "its manifest sets, so they are not compared with an earlier "
                                                      "round.",
    },
    ("measure_rear_pair", "Measure a rear pair round"): {
        evidence_reasons.REFUSE_NO_BRANCH_DIAGNOSTIC: "The rear pair round banked no branch diagnostic segments.",
        evidence_reasons.REFUSE_NO_REAR_TAKES: "The round has no usable rear summed takes.",
        evidence_reasons.REFUSE_NOT_A_REAR_PAIR: "The take is not a rear pair take, so it holds no woofer that "
                                                 "played alone and raw.",
        evidence_reasons.REFUSE_PAIR_UNDERSAMPLED: "The two woofers' relative phase turns more than a quarter turn "
                                                   "between two readings, so the fit cannot follow it.",
        evidence_reasons.REASON_POLARITY_SNR_SHORT: "No band holds both woofers far enough above the room's noise to read their polarity.",
        "rear_preview_needs_pair_round": "The rear preview needs a banked pair round, and there is none.",
    },
    ("name_target", "Name a target that covers the fit band"): {
        evidence_reasons.REFUSE_TARGET_BAND_SHORT: "The target document's valid band does not cover the band the "
                                                   "rear fit reads.",
    },
    ("measure_nearfield", "Measure a near-field round"): {
        evidence_reasons.REFUSE_NO_NEAR_FIELD_TAKES: "The round has no kept near-field driver takes.",
    },
    ("measure_classification_round", "Measure a verify or lateral round"): {
        evidence_reasons.NO_KEPT_TAKES: "The round kept none of its verify or lateral takes for the speaker.",
        evidence_reasons.ROUND_SHAPE_INADMISSIBLE: "The round banked no recording shape that feature classification can use.",
        "measurement_analysis_program_unsupported": "A take's program is not a one-channel verify sweep on a candidate "
                                                    "graph, so no banked analysis covers it.",
    },
    ("bank_round", "Bank this round again from its session"): {
        evidence_reasons.CAPTURE_UNREADABLE_SIDECAR: "This recording's sidecar is not a readable object with a phase.",
        evidence_reasons.EVIDENCE_NOT_BANKED: "This round's packet holds no evidence this build reads.",
        "capture_bundle_unavailable": "The run's session bundle is missing, or more than one matches, so its round was not banked.",
        "round_manifest_missing": "Bank the run manifest with this round.",
        "session_unfinished": "The session has not finished, so it cannot be banked yet.",
        "view_runner_unavailable": "The bank ran with no view runner, so it filed no round views.",
        "write_failed": "The round could not be written to the bank.",
    },
    ("select_round", "Name a round that holds takes this view can read"): {
        evidence_reasons.NO_ADMISSIBLE_CAPTURES: "The round holds no take this view can read.",
    },
    ("name_round", "Name a banked round or a live session bundle"): {
        "already_banked": "The session is already banked as a round.",
        "close_reference_unreadable_round": "The round directory named for the take is not a directory.",
        "not_a_bundle": "The directory is not a session bundle: it has no readable info.json object.",
        "round_ambiguous": "The id names a banked round and is also the name of another path.",
        "round_not_found": "The name is neither a banked round nor a live session bundle.",
    },
    ("name_frequencies", "Name the frequencies to classify"): {
        evidence_reasons.NO_FEATURES_DETECTED: "No feature in the pooled response rises above the scatter between recordings.",
    },
    ("measure_common_band", "Measure takes that cover a common band"): {
        evidence_reasons.REASON_COVERAGE_SHORT: "The captured takes do not cover the band this figure is read over.",
        "gate_sweep_reference_band_empty": "A take radiates nothing in the reference band, so its windows cannot share "
                                           "one level.",
        "handover_band_unmeasured": "The drivers were not measured across the handover band, so no trim is solved.",
        "no_common_frequency_support": "The candidate and incumbent room medians share no frequency range.",
    },
    ("name_comparand", "Name the take or forecast to compare with"): {
        "bass_comparand_view_not_filed": "The take the comparand rule found has no filed bass view: it is not a bass "
                                         "take, or its round filed none.",
        "compare_no_common_band": "The two sides share no band above the window's trusted floor.",
        "compare_no_comparand": "This round has no base take at this take's place, and no earlier banked take "
                                "matches its place, drivers and graph scope.",
        "compare_preview_unreadable": "The forecast named as side A is not a readable judge preview.",
        "compare_sample_rates_differ": "The two sides were recorded at different sample rates.",
    },
    ("choose_window", "Choose a longer window"): {
        "take_band_too_narrow": "The take's band above this window's trusted floor is too narrow to read.",
    },
    ("choose_window", "Choose a window inside the render"): {
        "dsp_replay_window_unavailable": "The requested window falls outside the render or is too short to read.",
    },
    ("render_again", "Render the graph again with dsp-replay"): {
        "bass_replay_manifest_predates_adr_0359": "This render's delivered output includes the retired volume taper, "
                                                  "so no stage isolates the compressor.",
    },
    ("review_evidence", "Review the evidence the view read"): {
        evidence_reasons.REASON_REFUSED: "The round view declined the evidence it read.",
        evidence_reasons.REASON_UNREADABLE: "The evidence this tool reads could not be read.",
        "field_malformed": "The artifact was read, and its field holds a value of the wrong type.",
        "field_null": "The artifact was read, and the field this block reads is empty.",
        "level_error": "The rear level could not be computed.",
        "source_absent": "The artifact this block reads was never supplied or banked.",
        "source_unreadable": "The artifact this block reads is there and cannot be read.",
        "unreadable": "The banked repeat floor cannot be read.",
        "unusable": "The banked repeat floor gives no stopping thresholds.",
    },
    ("choose_output", "Choose a writable output path"): {
        evidence_reasons.REASON_UNWRITABLE: "The tool could not write its output artifact.",
    },
    ("name_take", "Name a take this round banked"): {
        "close_reference_no_capture": "The round has no take with the named id, or more than one.",
        "room_capture_not_found": "No room take matches the named take id.",
        "room_capture_selection_required": "The round holds more than one room set, so a take id must choose one.",
        "round_take_not_kept": "Select a kept take, in this set or another; a refused attempt or level probe is banked, never read.",
        "round_take_selection_required": "Select a retained take from this set with the take selector.",
        "round_take_unknown": "Select a retained take from this set.",
    },
    ("measure_room", "Measure a new room round"): {
        "incompatible_measurement_basis": "The candidate and incumbent room medians were measured on different bases.",
        "room_incumbent_set_ambiguous": "More than one set of the run is a base, so the room has no one incumbent.",
        "room_incumbent_set_unavailable": "The run has no base set to grade the room against.",
        "room_median_unavailable": "The room median is missing, or this door cannot read it into limits.",
        "room_no_seat_takes": "The round has no readable room take at a seat, so no room median exists.",
    },
    ("select_candidate", "Select a candidate with one banked identity"): {
        "authored_candidate_conflict": "A different candidate is already banked under this identity.",
        "candidate_malformed": "The candidate is malformed, so it cannot be read.",
        "composition_base_required": "The document names no base: it needs a banked candidate fingerprint, or saved.",
        "composition_saved_tune_unrepresentable": "The saved driver corrections cannot be rebuilt as a candidate.",
        "gate_sweep_mixed_graphs": "The takes played more than one candidate or graph, so their windows cannot compare.",
    },
    ("match_bass_capture", "Measure both graphs at the same pose and settings"): {
        "capture_context_changed": "The two bass takes were captured under different conditions.",
    },
    ("measure_bass", "Measure a bass round"): {
        "bass_evidence_unavailable": "The round banked no bass reading or bass level for this prescription.",
    },
    ("register_mic_calibration", "Register microphone calibration"): {
        "mic_calibration_file_unreadable": "The calibration file cannot be read, is too large, or holds no calibration curve.",
        "mic_calibration_lookup_invalid": "The model and serial name no calibration that the vendor can look up.",
        "mic_calibration_none_registered": "No household microphone is registered.",
        "mic_calibration_store_unwritable": "The speaker's calibration folder cannot be written. Writing it needs sudo.",
        "mic_calibration_unavailable": "No calibration is available for the measurement microphone: none is "
                                       "remembered, or its file cannot be read.",
        "mic_calibration_unresolvable": "The registered microphone names a calibration that is no longer on the speaker.",
        "mic_calibration_vendor_link_off_host": "The vendor sent the lookup to another host, so it was not followed.",
        "mic_calibration_vendor_not_found": "The vendor holds no calibration for that serial.",
        "mic_calibration_vendor_unreachable": "The vendor could not be reached, so nothing was fetched.",
    },
    ("speaker_setup", "Finish the protected speaker setup"): {
        "audition_commission_load_active": "A per-driver setup config is loaded, so the applied tune is not what plays.",
        "audition_no_applied_profile": "No speaker tune is applied, so there is no graph to reduce.",
        "composition_saved_tune_unavailable": "The saved tune is not available to build on.",
        "driver_passband_unavailable": "The speaker declares no band for its drivers, so a per-driver prescription "
                                       "has nothing to check against.",
        "prescription_fc_unknown": "The crossover corner is unknown, so an alignment cannot be checked at it.",
    },
    ("speaker_setup", "Review speaker outputs"): {
        "aplay_failed": "The aplay tool failed, so the speaker cannot list its playback devices.",
        "aplay_missing": "The aplay tool is missing, so the speaker cannot list its playback devices.",
        "aplay_timeout": "The aplay tool timed out, so the speaker cannot list its playback devices.",
    },
    ("speaker_setup", "Review the protected speaker graph."): {
        "audition_applied_profile_displaced": "The saved applied tune is not the one the speaker plays.",
        "audition_emit_refused": "The reduced graph could not be built, or it failed its safety check.",
        "audition_malformed_graph": "The applied graph is malformed, so no comparison can be built.",
        "audition_no_rear_stage": "The applied graph has no single rear stage to compare.",
        "audition_running_graph_differs": "An unsaved EQ draft or another live edit is playing, so no comparison can start.",
        "cardioid_compare_unavailable": "The applied tune cannot compare the rear output.",
        "delay_graph_proof_failed": "The candidate's graph does not bind its delay to the outputs of the driver it names.",
        "tweeter_unprotected": "The candidate's graph leaves a tweeter output without its protective high-pass.",
    },
    ("review_candidate", "Review the candidate graph and driver declaration."): {
        "audition_rear_muted_in_tune": "The applied tune mutes the rear output, so there is nothing to compare.",
        "authored_status_required": "Only an unmeasured candidate can be authored.",
        # ``measured_crossover_candidate``'s field checks. The code and the detail name the field, so one sentence serves.
        **{code: "The candidate holds a value that is malformed, out of range or not supported." for code in (
            "alignment_invalid", "alignment_malformed", "alignment_partial", "analysis_invalid", "attenuation_out_of_range",
            "bass_extension_invalid", "bass_extension_malformed", "blend_correction_invalid", "blend_correction_malformed",
            "candidate_invalid", "candidate_schema_unsupported", "candidate_tampered", "delay_role_ambiguous",
            "delay_role_invalid", "delay_role_unknown", "delay_us_invalid", "delay_us_out_of_range",
            "effective_preset_invalid", "exclusion_evidence_invalid", "exclusion_evidence_malformed",
            "linearization_invalid", "linearization_malformed", "linearization_outcome_invalid",
            "linearization_outcome_malformed", "polarity_invalid", "program_id_invalid",
            "rear_calibration_case_unsupported", "rear_calibration_malformed", "rear_calibration_mode_unsupported",
            "role_attenuations_incomplete", "role_attenuations_malformed", "room_correction_invalid",
            "room_correction_malformed", "source_preset_invalid", "trim_decision_invalid", "trim_decision_malformed",
        )},
    },
    ("read_contract", "Read the section's contract"): {
        "above_lower_driver_band": "The crossover corner is above the band the lower driver declares.",
        "alignment_no_crossover_region": "A one-way speaker has no crossover, so there is nothing to align.",
        "alignment_prescription_schema_unsupported": "The alignment document names a schema version this build does not read.",
        "bass_compressor_attack_s_invalid": "The compressor attack time is not a finite number inside its allowed range.",
        "bass_compressor_factor_invalid": "The compressor factor is not a finite number inside its allowed range.",
        "bass_compressor_release_s_invalid": "The compressor release time is not a finite number inside its allowed range.",
        "bass_compressor_threshold_dbfs_invalid": "The compressor threshold is not a finite level inside its allowed range.",
        "bass_delta_highpass_hz_invalid": "The delta high-pass corner is not a finite number inside the measured band "
                                          "below the detector corner.",
        "bass_descriptor_malformed": "The bass section is not an object, or it names an unknown field, or it lacks a "
                                     "required one.",
        "bass_detector_lowpass_hz_invalid": "The detector low-pass corner is not a finite number inside the measured "
                                            "bass domain.",
        "bass_linkwitz_transform_invalid": "The Linkwitz transform is not an object of finite corners and Qs inside "
                                           "their bounds.",
        "below_declared_floor": "The crossover corner is below the floor the upper driver declares.",
        "boost_not_admitted": "The measured positions do not admit a boost at this frequency.",
        "boost_route_unavailable": "The blend stage carries no boost.",
        "composed_boost_exceeded": "The filters together boost more than the section's ceiling allows.",
        "composition_filters_invalid": "A driver's filters are not a list of filters for a driver role this speaker declares.",
        "composition_topology_required": "The hardware topology section cannot be cleared.",
        "driver_expectation_malformed": "The expected change or the tilt is not a finite number inside its bound.",
        "driver_filter_count_exceeded": "A driver role carries more filters than its branch may hold.",
        "driver_filter_malformed": "A driver filter, or the filter list, is not in the shape or the range the contract allows.",
        "driver_filter_outside_passband": "A driver filter boosts outside the band its driver declares.",
        "driver_filter_q_out_of_range": "A driver filter's Q is past the limit for a boost or a cut.",
        "driver_prescription_malformed": "The driver document is not in the shape the contract allows.",
        "driver_prescription_prohibited_field": "The driver document names a field it may not write: configuration, "
                                                "coefficients or a per-role level.",
        "driver_prescription_schema_unsupported": "The driver document names a schema version this build does not read.",
        "driver_prescription_too_large": "The driver document is larger than one driver document may be.",
        "driver_role_unknown": "The document names a driver role for which this speaker declares no band.",
        "driver_trim_pin_malformed": "The pinned trim does not name each driver role once, with a finite dB value "
                                     "inside its bound.",
        "filter_boost_too_high": "A filter boosts more than its section allows.",
        "filter_count_exceeded": "The document carries more filters than the contract allows.",
        "filter_malformed": "A filter, or the filter list, is not in the shape or the range the contract allows.",
        "filter_outside_region": "A filter sits outside the band its section allows.",
        "filter_q_out_of_range": "A filter's Q is outside the range its section allows.",
        "prescription_delay_invalid": "The alignment document states no delay, or a delay that is not a finite number.",
        "prescription_kind_unknown": "The document, or one of its sections, is not of a kind this build knows.",
        "prescription_malformed": "The document or the request is not in the shape the contract allows.",
        "prescription_outside_declared_window": "The delay is outside the window the preset declares for it.",
        "prescription_polarity_invalid": "The polarity is not one of the two words an alignment may pin.",
        "prescription_prohibited_field": "The document names a field it may not write: configuration, coefficients or a "
                                         "per-role value.",
        "prescription_schema_unsupported": "The document names a schema version this build does not read.",
        "prescription_section_unavailable": "The document names a section that this speaker's topology does not offer.",
        "prescription_section_unknown": "The document names a section this build does not know.",
        "prescription_too_large": "The document is larger than one document may be.",
        "rear_calibration_invalid": "The rear calibration section breaks a rule of the rear calibration document.",
        "rear_calibration_topology_unsupported": "The declared layout has no cabinet of one front woofer, one rear woofer "
                                                 "and one tweeter.",
        "region_unavailable": "The blend contract names no band, so a blend prescription has no band to check against.",
        "side_malformed": "The room document's sides are missing, repeated, or not the sides this speaker declares.",
        "strict_reader_disagreement": "The reader that loads persisted corrections would not vouch for this filter list, "
                                      "so it cannot be persisted.",
        "taper_violated": "The filters together boost the room past the taper the contract allows.",
        "topology_fc_invalid": "The crossover corner is missing, not a finite number, or not above zero.",
        "topology_malformed": "The topology cannot be checked: its document is malformed, or the declared drivers "
                              "give no crossover range.",
        "topology_no_crossover_region": "A one-way speaker has no crossover corner to change.",
        "topology_order_invalid": "The order is missing, or not an integer.",
        "topology_order_unsupported": "The order is not one this build can build.",
        "topology_prescription_schema_unsupported": "The topology document names a schema version this build does not read.",
        "topology_slope_below_declared_requirement": "The order's slope is below the minimum the protected driver publishes.",
    },
    ("read_catalog", "Read the tool catalog for the view and its inputs"): {
        "inputs_required": "This view needs inputs that the bank does not supply, so the bank did not run it.",
        "verb_not_registered": "No artifact row registers this view, so the bank did not run it.",
    },
    ("finish_measurement", "Review the active measurement"): {
        "audition_measurement_session_active": "A measurement holds the speaker's graph, so the audition cannot start.",
        "capture_slot_busy": "Another measurement holds the capture slot. Finish or cancel it, then join again.",
        "placement_refused": "The speaker refused the microphone position.",
        "position_mismatch": "The position that waits is not the one named.",
        "position_not_pending": "No microphone position waits for confirmation.",
        "round_manifest_unfinalized": "Wait for the run to finish.",
        "run_answer_invalid": "The speaker's answer to the run request names no run.",
        "run_not_current": "The speaker runs a different run than the one named.",
        "run_not_live": "The run is no longer live.",
        "stop_refused": "The speaker refused to stop the run.",
        "wait_timeout": "The wait ended before the run did.",
    },
    ("run_as_root", "Run it on the speaker as root"): {
        "dry_run_requires_local_host": "Dry-run reads this machine's facts. Run it on the speaker.",
        "local_state_unreadable": "The speaker's local state cannot be read by this user.",
        "not_root": "This tool runs only as root.",
    },
    ("name_value", "Name a value the tool accepts"): {
        "measurement_driver_not_offered": "The preset cannot play that driver alone here.",
        "measurement_layout_not_offered": "The preset does not offer that layout.",
        "measurement_poses_name_a_layout": "The poses name a layout: pass it as --layout.",
        "not_downloaded": "The wake model is not downloaded on this speaker.",
        "provider_unset": "No voice provider is selected yet.",
        "threshold_out_of_range": "The wake threshold is not a number from 0 to 1.",
        "unknown_model": "The tool offers no model with that name.",
        "unknown_provider": "The tool knows no voice provider with that name.",
        "unknown_voice": "The provider offers no voice with that name.",
        "unusable_value": "The value holds a character that a settings file cannot store.",
        "walk_refused": "The settle time is under the floor a landed arm needs, or the poll interval is not above zero.",
    },
    ("check_settings_file", "Check the settings file"): {
        "save_failed": "The setting could not be written to its file.",
        "settings_unreadable": "A settings file could not be read.",
    },
    ("restart_voice", "Restart the voice service"): {
        "restart_refused": "The setting is saved, and the restart of the voice service was refused.",
    },
    ("run_program", "Name the program to run"): {
        "trial_program_unknown": "No measurement program covers a section that this candidate changes.",
    },
    ("check_speaker", "Check that the speaker answers, then read its state"): {
        "answer_lost": "The speaker gave no usable answer.",
        "apply_not_applied": "The speaker did not report the candidate as applied.",
        "audition_load_refused": "CamillaDSP did not load the graph.",
        "audition_no_durable_anchor": "CamillaDSP reports no saved graph to put back, so the audition does not start.",
        "authored_candidate_unreadable": "The candidate was written and cannot be read back.",
        "run_refused": "The speaker refused to open the run.",
        "status_unavailable": "The speaker gave no run status.",
    },
    ("stop_audition", "Put the full graph back with jasper-audition stop"): {
        "audition_not_restored": "The audition ended, and the speaker is not back on its full graph.",
        "audition_restore_failed": "The reduced graph still plays, and the applied graph did not reload.",
    },
    ("name_set", "Name a set this round banked"): {
        "round_set_unknown": "Select a set listed in the run manifest.",
        "set_required": "Name --set with one of the listed set ids.",
    },
    ("add_api_key", "Add the provider's API key on the voice page"): {
        "key_unset": "The voice provider has no API key set.",
    },
}

#: The evidence store's failure codes. ``plan_run.failure_reason`` keeps the registered code of an exception that
#: stops a run, so the household reads these, and the failure envelope answers a session restart with Start over.
_STORE_COPY: dict[str, str] = {
    "commissioning_evidence_insufficient_space": "The speaker has too little free space to save this measurement, so free "
                                                 "space before measuring again.",
    "commissioning_evidence_integrity_mismatch": "A saved measurement file does not match its record, or cannot be read.",
    "commissioning_evidence_invalid_path": "A saved measurement file has a path the speaker does not accept.",
    "commissioning_evidence_malformed": "A saved measurement file is not valid.",
    "commissioning_evidence_missing": "A saved measurement file is missing.",
    "commissioning_evidence_not_canonical": "A saved measurement file is not in the exact form the speaker writes.",
    "commissioning_evidence_not_regular": "A saved measurement entry is a link or another kind of entry, not a regular file.",
    "commissioning_evidence_path_conflict": "A saved measurement file already holds different data, and a saved file is "
                                            "written only once.",
    "commissioning_evidence_persist_failed": "The speaker could not save this measurement.",
    "commissioning_evidence_persist_outcome_unknown": "The speaker could not confirm that this measurement was saved.",
    "commissioning_evidence_too_large": "A saved measurement file is larger than its size limit.",
    "commissioning_evidence_total_too_large": "This session's saved measurements are at their total size limit.",
    "commissioning_evidence_wrong_authority": "The saved measurements belong to another session, or the session cannot "
                                              "be opened.",
}


# The §5.10 table, as data. The envelope and the session both read it, so
# copy and budget never drift between the verdict and its screen.
REASON_REGISTRY: dict[str, ReasonSpec] = {
    **{code: ReasonSpec(code, TEMPLATE_FIX_AND_RETRY, 0, "", message)
       for code, message in (
           ("level_unreachable", "The target level is unreachable at this gain. Check the amplifier and microphone."),
           ("level_ambient_too_high", "The room is too loud to level. Reduce the ambient noise and try again."),
           ("spl_level_unsettled", "The microphone level did not settle. Try again."),
           ("mic_not_observing", "The microphone did not hear the speaker. Check its position and connection."),
           ("mic_feed_lost", "The microphone stopped sending samples. Check its connection and try again."),
           ("mic_clipping", "The microphone clipped. Check the microphone and lower the level."),
           ("volume_latch_unconfirmed", "The amplifier gain could not be confirmed. Check the audio connection."),
           ("fader_above_cap", "The amplifier gain exceeds the 0 dB cap. Lower it before leveling."),
           ("spl_target_uncapturable", "The microphone cannot measure the requested level. Use a suitable microphone."),
           ("seat_level_watchdog_expired", "Leveling timed out. Check the audio connection and try again."),
       )},
    "bass_fit_capture_context_changed": ReasonSpec(
        "bass_fit_capture_context_changed", TEMPLATE_HARD_STOP, 0, "", "The paired bass captures used different conditions.",
        next_action={"id": "match_bass_capture", "label": "Measure both graphs at the same pose and settings", "href": "/sound/speaker/crossover/"},
    ),
    "bass_fit_reference_band_unavailable": ReasonSpec(
        "bass_fit_reference_band_unavailable", TEMPLATE_HARD_STOP, 0, "", "The bass sweep does not cover the reference band.",
        next_action={"id": "measure_bass_reference", "label": "Measure a sweep that covers the reference band", "href": "/sound/speaker/crossover/"},
    ),
    "bass_fit_requires_room_baseline_and_exact_candidate": ReasonSpec(
        "bass_fit_requires_room_baseline_and_exact_candidate", TEMPLATE_HARD_STOP, 0, "", "The bass pair does not contain the required graphs.",
        next_action={"id": "select_bass_pair", "label": "Select the room baseline and the measured bass candidate", "href": "/sound/speaker/crossover/"},
    ),
    "bass_fit_inputs_missing": ReasonSpec(
        "bass_fit_inputs_missing", TEMPLATE_HARD_STOP, 0, "", "No bass pairs are available for this fit.",
        next_action={"id": "select_bass_run", "label": "Select a run with baseline and candidate takes", "href": "/sound/speaker/crossover/"},
    ),
    "bass_fit_pose_missing": ReasonSpec(
        "bass_fit_pose_missing", TEMPLATE_HARD_STOP, 0, "", "The bass capture has no recorded pose.",
        next_action={"id": "measure_bass_pose", "label": "Measure with a recorded microphone pose", "href": "/sound/speaker/crossover/"},
    ),
    "bass_table_window_gain_missing": ReasonSpec(
        "bass_table_window_gain_missing", TEMPLATE_HARD_STOP, 0, "", "The bass capture lacks a complete resolved window gain.",
        next_action={"id": "measure_bass_level", "label": "Record Main and program identity on each take", "href": "/sound/speaker/crossover/"},
    ),
    "bass_table_capture_integrity_failed": ReasonSpec(
        "bass_table_capture_integrity_failed", TEMPLATE_HARD_STOP, 0, "", "A bass capture failed its integrity check.",
        next_action={"id": "repeat_bass_capture", "label": "Repeat the failed capture", "href": "/sound/speaker/crossover/"},
    ),
    "bass_table_capture_context_changed": ReasonSpec(
        "bass_table_capture_context_changed", TEMPLATE_HARD_STOP, 0, "", "The bass levels were captured under different conditions.",
        next_action={"id": "match_bass_levels", "label": "Measure all levels with the same stimulus and setup", "href": "/sound/speaker/crossover/"},
    ),
    "bass_fit_pairs_unavailable": ReasonSpec(
        "bass_fit_pairs_unavailable", TEMPLATE_HARD_STOP, 0, "", "The run has no unique baseline pair for each candidate take.",
        next_action={"id": "complete_bass_pairs", "label": "Measure baseline and candidates at matching levels and poses", "href": "/sound/speaker/crossover/"},
    ),
    "bass_fit_candidate_unreadable": ReasonSpec(
        "bass_fit_candidate_unreadable", TEMPLATE_HARD_STOP, 0, "", "The measured bass candidate descriptor is unavailable.",
        next_action={"id": "select_bass_candidate", "label": "Supply the candidate artifact named by the run", "href": "/sound/speaker/crossover/"},
    ),
    "bass_fit_run_mismatch": ReasonSpec(
        "bass_fit_run_mismatch", TEMPLATE_HARD_STOP, 0, "", "The selected run does not match this manifest.",
        next_action={"id": "select_bass_run", "label": "Select the run recorded in this manifest", "href": "/sound/speaker/crossover/"},
    ),
    # See ADR-0371
    "room_not_banked": ReasonSpec(
        "room_not_banked", TEMPLATE_HARD_STOP, 0, "", "This round banked no room measurement.",
        next_action={"id": "measure_room", "label": "Measure a new room round", "href": "/sound/speaker/crossover/"},
    ),
    **{code: ReasonSpec(code, TEMPLATE_HARD_STOP, 0, "", message,
                        next_action={"id": action, "label": label, "href": "/sound/speaker/crossover/"})
       for (action, label), rows in _EVIDENCE_COPY.items() for code, message in rows.items()},
    **{code: ReasonSpec(code, TEMPLATE_SESSION_RESTART, 0, "", message,
                        next_action={"id": "measure_again", "label": "Measure this round again",
                                     "href": "/sound/speaker/crossover/"})
       for code, message in _STORE_COPY.items()},
    **{code: ReasonSpec(code, TEMPLATE_HARD_STOP, 0, "", label,
                       next_action={"id": action, "label": label, "href": "/sound/speaker/crossover/"})
       for code, action, label in (
           ("compose_refused", "review_candidate", "Review the candidate graph and driver declaration."),
           ("composition_invalid", "review_candidate", "Review the candidate graph and driver declaration."),
           ("reset_compose_failed", "review_candidate", "Review the candidate graph and driver declaration."),
           ("program_headroom_exhausted", "reduce_boosts", "Reduce the room, driver or rear boosts, or lower the Extra headroom setting."),
           ("crossover_below_declared_protection_floor", "raise_crossover", "Raise the crossover to the declared driver protection floor."),
           ("baseline_graph_safety_proof_failed", "speaker_setup", "Review the protected speaker graph."),
           ("baseline_config_validation_failed", "speaker_setup", "Review the protected speaker graph."),
       )},
    "wired_mic_missing": ReasonSpec(
        "wired_mic_missing", TEMPLATE_HARD_STOP, 0, "", "Connect the measurement microphone.",
        next_action={"id": "connect_mic", "label": "Connect the measurement microphone", "href": "/sound/speaker/crossover/"},
    ),
    CODE_CAPTURE_GAIN_UNVERIFIED: ReasonSpec(
        CODE_CAPTURE_GAIN_UNVERIFIED, TEMPLATE_HARD_STOP, 0, "",
        "JTS could not set the measurement microphone's input level to full, so it cannot check the sound level. "
        "Reconnect the microphone, then measure again.",
        next_action={"id": "connect_mic", "label": "Reconnect the measurement microphone", "href": "/sound/speaker/crossover/"},
    ),
    "measurement_mic_unidentified": ReasonSpec(
        "measurement_mic_unidentified", TEMPLATE_HARD_STOP, 0, "", "Select a known measurement microphone.",
        next_action={"id": "identify_mic", "label": "Select a known measurement microphone", "href": "/sound/speaker/crossover/"},
    ),
    "measure_box_not_ready": ReasonSpec(
        "measure_box_not_ready", TEMPLATE_HARD_STOP, 0, "", "Finish the protected speaker setup.",
        next_action={"id": "speaker_setup", "label": "Finish the protected speaker setup", "href": "/sound/speaker/crossover/"},
    ),
    "seat_anchor_unusable": ReasonSpec(
        "seat_anchor_unusable", TEMPLATE_HARD_STOP, 0, "", "Run jasper-seat-level with the current microphone, then measure.",
        next_action={"id": "measure_seat_level", "label": "Run jasper-seat-level with the current microphone, then measure", "href": "/sound/speaker/crossover/"},
    ),
    DECLARED_GEOMETRY_UNREADABLE: ReasonSpec(
        DECLARED_GEOMETRY_UNREADABLE, TEMPLATE_HARD_STOP, 0, "",
        "Declare the rig again with jasper-declare-geometry set; jasper-declare-geometry show prints the command.",
        next_action={"id": "declare_geometry", "label": "Declare the rig again with jasper-declare-geometry set", "href": "/sound/speaker/crossover/"},
    ),
    "not_found": ReasonSpec(
        "not_found", TEMPLATE_HARD_STOP, 0, "", "Select a candidate from the bank.",
        next_action={"id": "select_candidate", "label": "Select a candidate from the bank", "href": "/sound/speaker/crossover/"},
    ),
    "ambiguous": ReasonSpec(
        "ambiguous", TEMPLATE_HARD_STOP, 0, "", "Select a candidate with one banked identity.",
        next_action={"id": "select_candidate", "label": "Select a candidate with one banked identity", "href": "/sound/speaker/crossover/"},
    ),
    "fingerprint_required": ReasonSpec(
        "fingerprint_required", TEMPLATE_HARD_STOP, 0, "", "Supply a candidate fingerprint.",
        next_action={"id": "select_candidate", "label": "Supply a candidate fingerprint", "href": "/sound/speaker/crossover/"},
    ),
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
        capture_quality=True,
    ),
    REASON_NOISY_ROOM_LINEARITY: _retriable_reason(
        REASON_NOISY_ROOM_LINEARITY, TEMPLATE_FIX_AND_RETRY, 1,
        RetryableReasonCopy(
            "The room got loud during that measurement.",
            "quiet it and try again.",
            joiner=" — ",
            strip_before_join=".",
        ),
        capture_quality=True,
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
        capture_quality=True,
    ),
    REASON_SNR_FLOOR: _retriable_reason(
        REASON_SNR_FLOOR, TEMPLATE_FIX_AND_RETRY, 1,
        RetryableReasonCopy(
            "The room is too loud right now, or the microphone is too far away.",
            "Quiet the room or move the microphone closer, then try again.",
        ),
        capture_quality=True,
    ),
    REASON_CHANNEL_MAP_MISMATCH: ReasonSpec(
        REASON_CHANNEL_MAP_MISMATCH, TEMPLATE_HARD_STOP, 0, "",
        # The numbers behind the refusal are on
        # `event=correction.crossover_v2_check_diag`, which publishes each
        # role's raw rises, isolation ratio, and bound.
        channel_map_mismatch_message(()),
    ),
    REASON_ANCHOR_AMBIGUOUS: _retriable_reason(
        REASON_ANCHOR_AMBIGUOUS, TEMPLATE_FIX_AND_RETRY, 1,
        # About the RECORDING and not the speaker: naming a cause in the
        # speaker would be an over-claim. Re-recording clears it, because the
        # anchor collapse is a property of one take.
        RetryableReasonCopy(
            "JTS couldn't line that recording up with the test tones it played.",
            "Try that measurement again.",
        ),
        capture_quality=True,
    ),
    REASON_PILOT_STEP_IMPLAUSIBLE: _retriable_reason(
        REASON_PILOT_STEP_IMPLAUSIBLE, TEMPLATE_FIX_AND_RETRY, 1,
        RetryableReasonCopy(
            "The two level-check tones did not differ by the programmed step, so this recording cannot be trusted.",
            "Take it again.",
        ),
        capture_quality=True,
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
    REASON_LEVEL_DRIFT_AT_SESSION_GAIN: _retriable_reason(
        REASON_LEVEL_DRIFT_AT_SESSION_GAIN, TEMPLATE_FIX_AND_RETRY, 1,
        RetryableReasonCopy("The microphone read a different level at the same gain — something changed in the room.",
                            "Retake."),
        capture_quality=True,
    ),
    REASON_LEVEL_OFF_TARGET: _retriable_reason(
        REASON_LEVEL_OFF_TARGET, TEMPLATE_SILENT_AUTO_RETRY, 1,
        RetryableReasonCopy("That was not at the measuring level.", "measuring again at the right level.",
                            joiner=" — ", strip_before_join="."),
        auto_retry=True, capture_quality=True,
    ),
    REASON_CAPTURE_OVERRUN: _retriable_reason(
        REASON_CAPTURE_OVERRUN, TEMPLATE_SILENT_AUTO_RETRY, 1,
        RetryableReasonCopy("JTS was busy and missed part of the recording.", "measuring again.",
                            joiner=" — ", strip_before_join="."),
        auto_retry=True, capture_quality=True,
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
        capture_quality=True,
    ),
    REASON_SWEEP_MISSING: _retriable_reason(
        REASON_SWEEP_MISSING, TEMPLATE_SILENT_AUTO_RETRY, 1,
        RetryableReasonCopy(
            "JTS heard the test tones, but not the sweep after them.",
            "measuring again.",
            joiner=" — ",
            strip_before_join=".",
        ),
        auto_retry=True,
        capture_quality=True,
    ),
    REASON_DELAY_EXCEEDS_SEARCH_WINDOW: _retriable_reason(
        REASON_DELAY_EXCEEDS_SEARCH_WINDOW, TEMPLATE_FIX_AND_RETRY, 1,
        RetryableReasonCopy(
            "The microphone may be off the spot in the picture.",
            "Re-check its placement, then try again.",
        ),
        capture_quality=True,
    ),
    REASON_LOCATE_FAILED: _retriable_reason(
        REASON_LOCATE_FAILED, TEMPLATE_FIX_AND_RETRY, 1,
        RetryableReasonCopy("Couldn't hear the speaker clearly.", LOCATE_RETRY_ACTION),
        capture_quality=True,
    ),
    REASON_VOLUME_UNRESOLVED: ReasonSpec(
        REASON_VOLUME_UNRESOLVED, TEMPLATE_VOLUME_RECOVERY, 0, "",
        "JTS could not confirm the listening volume was restored. Recover the "
        "safe volume before continuing.",
        next_action={"id": "recover_volume", "label": "Recover safe listening volume",
                     "href": "/sound/speaker/crossover/"},
    ),
    "driver_protection_invalid": ReasonSpec(
        "driver_protection_invalid", TEMPLATE_HARD_STOP, 0, "",
        "The driver protection confirmed in speaker setup cannot be used for "
        "this measurement. Review the driver limits, then measure again.",
        next_action={"id": "review_safety_limits", "label": "Review driver limits",
                     "href": "/sound/speaker/#driver-safety-issues"},
    ),
    "program_admission_refused": ReasonSpec(
        "program_admission_refused", TEMPLATE_HARD_STOP, 0, "",
        "The speaker's safety limits refused this sweep. Nothing was played for the refused sweep. "
        "Correct the sweep band, level or length before retrying.",
    ),
    "session_level_not_ready": ReasonSpec(
        "session_level_not_ready", TEMPLATE_SESSION_RESTART, 0, "",
        "The measurement volume was not ready. Nothing was played for this sweep. "
        "Check the speaker's volume status, then start the measurement again.",
    ),
    "program_play_failed": ReasonSpec(
        "program_play_failed", TEMPLATE_SESSION_RESTART, 0, "",
        "The speaker could not play the measurement program. Check the playback status before retrying.",
    ),
    **{code: ReasonSpec(code, TEMPLATE_SESSION_RESTART, 0, "", message + " The remaining poses were not measured.")
       for code, message in ARM_STOP_COPY.items()},
    REASON_PROGRAM_UNPLAYABLE: ReasonSpec(
        REASON_PROGRAM_UNPLAYABLE, TEMPLATE_HARD_STOP, 0, "",
        "JTS could not play the measurement signal within the speaker's safe "
        "limits. Re-check the driver details in speaker setup, then measure "
        "again.",
    ),
    REASON_PROGRAM_PLAN_SHAPE_INVALID: ReasonSpec(
        REASON_PROGRAM_PLAN_SHAPE_INVALID, TEMPLATE_HARD_STOP, 0, "",
        "JTS could not read the measurement plan. Submit a complete plan in the current format.",
        next_action={
            "id": "review_plan",
            "label": "Review measurement settings",
            "href": "/sound/speaker/crossover/",
        },
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
    REASON_MEASUREMENT_OUTPUT_MUTED: ReasonSpec(
        REASON_MEASUREMENT_OUTPUT_MUTED, TEMPLATE_HARD_STOP, 0, "",
        "The speaker is muted. Raise the speaker volume above zero, then measure again.",
        next_action={"id": "raise_volume", "label": "Raise speaker volume above zero", "href": "/sound/"},
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
    REASON_PROGRAM_MEASUREMENT_INPUTS_INVALID: ReasonSpec(
        REASON_PROGRAM_MEASUREMENT_INPUTS_INVALID, TEMPLATE_HARD_STOP, 0, "",
        "The driver limits needed for measurement are missing or do not fit. "
        "Check the listed driver issues in speaker setup before measuring.",
        next_action={"id": "review_safety_limits", "label": "Review driver limits",
                     "href": "/sound/speaker/#driver-safety-issues"},
    ),
    REASON_DRIVER_SENSITIVITY_UNDECLARED: ReasonSpec(
        REASON_DRIVER_SENSITIVITY_UNDECLARED, TEMPLATE_HARD_STOP, 0, "",
        "JTS sets a tweeter's measurement level from the declared driver sensitivities, and one is "
        "missing or differs between a driver's outputs. Declare one sensitivity for each driver in "
        "speaker setup, then measure again.",
        next_action={"id": "declare_driver_sensitivity", "label": "Declare this driver's sensitivity",
                     "href": "/sound/speaker/"},
    ),
    REASON_MEASUREMENT_TARGETS_MISSING: ReasonSpec(
        REASON_MEASUREMENT_TARGETS_MISSING, TEMPLATE_HARD_STOP, 0, "",
        "JTS does not have a measurement target for every driver this speaker "
        "declares, so it cannot measure them. Finish speaker setup so each "
        "driver is assigned to an output, then measure again.",
        next_action={
            "id": "speaker_setup",
            "label": "Finish speaker setup",
            "href": "/sound/speaker/",
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
            "href": "/sound/speaker/",
        },
    ),
    # Measurement graph and walk refusals (tracking issue #4942).
    REASON_MEASUREMENT_GRAPH_UNAVAILABLE: ReasonSpec(
        REASON_MEASUREMENT_GRAPH_UNAVAILABLE, TEMPLATE_HARD_STOP, 0, "",
        "JTS could not install or restore the measurement audio setup. Check the speaker's audio state, then start a new session.",
        next_action={"id": "new_measurement_session", "label": "Start a new session",
                     "href": "/sound/speaker/crossover/"},
    ),
    REASON_MEASUREMENT_BASELINE_UNAVAILABLE: ReasonSpec(
        REASON_MEASUREMENT_BASELINE_UNAVAILABLE, TEMPLATE_HARD_STOP, 0, "",
        "JTS could not build this program's baseline. Review the saved speaker setup before measuring.",
        next_action={"id": "speaker_setup", "label": "Review speaker setup", "href": "/sound/speaker/"},
    ),
    REASON_MEASUREMENT_CANDIDATE_SPEAKER_MISMATCH: ReasonSpec(
        REASON_MEASUREMENT_CANDIDATE_SPEAKER_MISMATCH, TEMPLATE_HARD_STOP, 0, "",
        "The selected tuning uses a different speaker setup. Select a tuning for this speaker.",
        next_action={"id": "speaker_setup", "label": "Review speaker outputs", "href": "/sound/speaker/"},
    ),
    REASON_MEASUREMENT_CANDIDATE_REQUIRED: ReasonSpec(
        REASON_MEASUREMENT_CANDIDATE_REQUIRED, TEMPLATE_HARD_STOP, 0, "",
        'This measurement needs a saved tuning to test. Select the tuning, then measure again.',
        next_action={"id": 'select_candidate', "label": 'Select a tuning',
                     "href": '/sound/speaker/crossover/'},
    ),
    REASON_MEASUREMENT_PROGRAM_NOT_OFFERED: ReasonSpec(
        REASON_MEASUREMENT_PROGRAM_NOT_OFFERED, TEMPLATE_HARD_STOP, 0, "",
        "This speaker does not offer that measurement. Choose one from the list.",
    ),
    REASON_MEASUREMENT_CANDIDATE_INVALID: ReasonSpec(
        REASON_MEASUREMENT_CANDIDATE_INVALID, TEMPLATE_HARD_STOP, 0, "",
        'JTS cannot read the selected tuning. Select a valid saved tuning, then measure again.',
        next_action={"id": 'select_candidate', "label": 'Select a valid tuning',
                     "href": '/sound/speaker/crossover/'},
    ),
    REASON_MEASUREMENT_SCOPE_INVALID: ReasonSpec(
        REASON_MEASUREMENT_SCOPE_INVALID, TEMPLATE_HARD_STOP, 0, "",
        'JTS cannot measure the selected tuning layer. Select a supported measurement layer.',
        next_action={"id": 'select_measurement_scope', "label": 'Select a measurement layer',
                     "href": '/sound/speaker/crossover/'},
    ),
    REASON_MEASUREMENT_FILTERS_INVALID: ReasonSpec(
        REASON_MEASUREMENT_FILTERS_INVALID, TEMPLATE_HARD_STOP, 0, "",
        'JTS cannot read all the filters in this tuning. Select a valid saved tuning before '
        'measuring again.',
        next_action={"id": 'select_candidate', "label": 'Select a valid tuning',
                     "href": '/sound/speaker/crossover/'},
    ),
    REASON_MEASUREMENT_BRANCH_CHANNELS: ReasonSpec(
        REASON_MEASUREMENT_BRANCH_CHANNELS, TEMPLATE_HARD_STOP, 0, "",
        'This measurement needs the woofer and tweeter on separate supported outputs. Review their '
        'output assignments in speaker setup.',
        next_action={"id": 'speaker_setup', "label": 'Review speaker outputs',
                     "href": '/sound/speaker/'},
    ),
    REASON_WALK_RIG_CLEAR_NOT_ATTESTED: ReasonSpec(
        REASON_WALK_RIG_CLEAR_NOT_ATTESTED, TEMPLATE_HARD_STOP, 0, "",
        "Confirm that the arm's full sweep path is clear with --attest-rig-clear.",
        next_action={"id": "attest_rig_clear", "label": "Check the full sweep path and attest",
                     "href": "/sound/speaker/crossover/"},
    ),
    REASON_ARM_PARK_UNCONFIRMED: ReasonSpec(
        REASON_ARM_PARK_UNCONFIRMED, TEMPLATE_HARD_STOP, 0, "",
        "Check the arm and its parked journal row before starting another round.",
        next_action={"id": "check_arm_park", "label": "Check the arm park",
                     "href": "/sound/speaker/crossover/"},
    ),
    REASON_WALK_MOVER_UNAVAILABLE: ReasonSpec(
        REASON_WALK_MOVER_UNAVAILABLE, TEMPLATE_HARD_STOP, 0, "",
        "Connect the arm adapter and check that root can detect it.",
        next_action={"id": "connect_arm", "label": "Connect and detect the arm adapter",
                     "href": "/sound/speaker/crossover/"},
    ),
    REASON_WALK_MOVER_MISMATCH: ReasonSpec(
        REASON_WALK_MOVER_MISMATCH, TEMPLATE_HARD_STOP, 0, "",
        'Match the microphone movement settings in the plan and session.',
        next_action={"id": 'match_walk_mover', "label": 'Match the movement settings',
                     "href": '/sound/speaker/crossover/'},
    ),
    REASON_WALK_OVER_MOVER_ENVELOPE: ReasonSpec(
        REASON_WALK_OVER_MOVER_ENVELOPE, TEMPLATE_HARD_STOP, 0, "",
        'A measurement position is beyond the stated movement range. Move that position within the '
        'range.',
        next_action={"id": 'adjust_walk_positions', "label": 'Adjust the positions',
                     "href": '/sound/speaker/crossover/'},
    ),
    REASON_WALK_LEVEL_POLICY_INVALID: ReasonSpec(
        REASON_WALK_LEVEL_POLICY_INVALID, TEMPLATE_HARD_STOP, 0, "",
        'Correct the measurement level settings before starting.',
        next_action={"id": 'correct_walk_levels', "label": 'Correct the level settings',
                     "href": '/sound/speaker/crossover/'},
    ),
    REASON_RUN_LEVEL_PILOTS_UNDER_AMBIENT: ReasonSpec(
        REASON_RUN_LEVEL_PILOTS_UNDER_AMBIENT, TEMPLATE_HARD_STOP, 0, "",
        'The requested level puts the summed pilots below the required signal-to-noise floor.',
        next_action={"id": 'correct_walk_levels', "label": 'Choose a level within the measurement limits',
                     "href": '/sound/speaker/crossover/'},
    ),
    REASON_VOLUME_RESTORE_DEFERRED: ReasonSpec(
        REASON_VOLUME_RESTORE_DEFERRED, TEMPLATE_HARD_STOP, 0, "",
        'Measurement stopped because another volume claim is active.',
        next_action={"id": "new_measurement_session", "label": "Start a new measurement after playback settles",
                     "href": "/sound/speaker/crossover/"},
    ),
    REASON_WALK_SCHEMA_VERSION_UNSUPPORTED: ReasonSpec(
        REASON_WALK_SCHEMA_VERSION_UNSUPPORTED, TEMPLATE_HARD_STOP, 0, "",
        'Submit the measurement plan in the current request format.',
        next_action={"id": "review_plan", "label": "Review measurement settings",
                     "href": "/sound/speaker/crossover/"},
    ),
    REASON_MEASURE_SPL_CALIBRATION_REQUIRED: ReasonSpec(
        REASON_MEASURE_SPL_CALIBRATION_REQUIRED, TEMPLATE_HARD_STOP, 0, "",
        'JTS needs microphone calibration to check the sound level during this measurement. '
        'Register calibration with microphone sensitivity, then measure again.',
        next_action={"id": 'register_mic_calibration', "label": 'Register microphone calibration',
                     "href": '/sound/speaker/crossover/'},
    ),
    REASON_WALK_COMMISSIONING_STOP_UNSET: ReasonSpec(
        REASON_WALK_COMMISSIONING_STOP_UNSET, TEMPLATE_HARD_STOP, 0, "",
        'This speaker has no sound level stop set for measurements. Set the stop level in speaker '
        'setup before measuring.',
        next_action={"id": 'speaker_setup', "label": 'Set the measurement stop level',
                     "href": '/sound/speaker/'},
    ),
    REASON_WALK_STIMULUS_NOT_ACCEPTED: ReasonSpec(
        REASON_WALK_STIMULUS_NOT_ACCEPTED, TEMPLATE_HARD_STOP, 0, "",
        'The test signal does not fit this measurement plan. Choose a supported signal for its '
        'positions.',
        next_action={"id": 'correct_walk_stimulus', "label": 'Correct the test signal',
                     "href": '/sound/speaker/crossover/'},
    ),
    REASON_WALK_OVER_CAPTURE_CAPACITY: ReasonSpec(
        REASON_WALK_OVER_CAPTURE_CAPACITY, TEMPLATE_HARD_STOP, 0, "",
        'This plan has more recordings than one session can hold. Split the positions across '
        'separate sessions.',
        next_action={"id": 'split_walk', "label": 'Split the measurement plan',
                     "href": '/sound/speaker/crossover/'},
    ),
    REASON_WALK_STOP_NO_LONGER_VALID: ReasonSpec(
        REASON_WALK_STOP_NO_LONGER_VALID, TEMPLATE_HARD_STOP, 0, "",
        'A saved measurement position is no longer valid. Correct that position before starting.',
        next_action={"id": 'correct_walk_stop', "label": 'Correct the saved position',
                     "href": '/sound/speaker/crossover/'},
    ),
    REASON_WALK_TEMPLATE_NOT_ACCEPTED: ReasonSpec(
        REASON_WALK_TEMPLATE_NOT_ACCEPTED, TEMPLATE_HARD_STOP, 0, "",
        'The test signal settings include position fields that the plan must set. Remove those '
        'fields from the signal settings.',
        next_action={"id": 'correct_walk_template', "label": 'Correct the signal settings',
                     "href": '/sound/speaker/crossover/'},
    ),
    REASON_WALK_POLARITY_NOT_ACCEPTED: ReasonSpec(
        REASON_WALK_POLARITY_NOT_ACCEPTED, TEMPLATE_HARD_STOP, 0, "",
        'The selected driver and polarity settings do not match. Correct the polarity settings '
        'before starting.',
        next_action={"id": 'correct_walk_polarity', "label": 'Correct the polarity settings',
                     "href": '/sound/speaker/crossover/'},
    ),
    REASON_WALK_DELAY_NOT_ACCEPTED: ReasonSpec(
        REASON_WALK_DELAY_NOT_ACCEPTED, TEMPLATE_HARD_STOP, 0, "",
        'The selected driver and delay settings do not match. Correct the delay settings before '
        'starting.',
        next_action={"id": 'correct_walk_delay', "label": 'Correct the delay settings',
                     "href": '/sound/speaker/crossover/'},
    ),
    REASON_WALK_LEVEL_MATCH_NO_EVIDENCE: ReasonSpec(
        REASON_WALK_LEVEL_MATCH_NO_EVIDENCE, TEMPLATE_HARD_STOP, 0, "",
        'JTS has no measured driver levels to match. Measure the driver levels before asking it to '
        'match them.',
        next_action={"id": 'measure_driver_levels', "label": 'Measure the driver levels',
                     "href": '/sound/speaker/crossover/'},
    ),
    REASON_WALK_CANDIDATE_NOT_MEASURABLE: ReasonSpec(
        REASON_WALK_CANDIDATE_NOT_MEASURABLE, TEMPLATE_HARD_STOP, 0, "",
        "A summed tuning test must use that tuning's own levels and alignment. Remove the separate "
        'level or alignment overrides.',
        next_action={"id": 'remove_trial_overrides', "label": "Use the tuning's own settings",
                     "href": '/sound/speaker/crossover/'},
    ),
    REASON_WALK_BRANCH_PAIR_UNDECLARED: ReasonSpec(
        REASON_WALK_BRANCH_PAIR_UNDECLARED, TEMPLATE_HARD_STOP, 0, "",
        'This measurement plays a driver output this speaker does not declare. Check the outputs '
        'in speaker setup, then measure again.',
        next_action={"id": 'speaker_setup', "label": 'Open speaker setup',
                     "href": '/sound/speaker/'},
    ),
    REASON_WALK_LAYOUT_UNSUPPORTED_FOR_PER_DRIVER_PROGRAMS: ReasonSpec(
        REASON_WALK_LAYOUT_UNSUPPORTED_FOR_PER_DRIVER_PROGRAMS, TEMPLATE_HARD_STOP, 0, "",
        'This layout declares three driver roles: woofer, mid and tweeter. '
        'The measurement programs are not built for it yet.',
    ),
    REASON_WALK_NOTHING_PLAYABLE: ReasonSpec(
        REASON_WALK_NOTHING_PLAYABLE, TEMPLATE_HARD_STOP, 0, "",
        'This plan contains only separate driver measurements, which this runner cannot play. Run '
        'it through the guided speaker measurement.',
        next_action={"id": 'guided_measurement', "label": 'Open guided measurement',
                     "href": '/sound/speaker/crossover/'},
    ),
    # End measurement graph and walk refusals.
    REASON_SPL_CEILING_EXCEEDED: ReasonSpec(
        REASON_SPL_CEILING_EXCEEDED, TEMPLATE_HARD_STOP, 0, "",
        "The measurement stopped because the microphone heard the speaker "
        "louder than the commissioning stop. Lower the level and "
        "measure again.",
    ),
    REASON_INTERNAL_ERROR: ReasonSpec(
        REASON_INTERNAL_ERROR, TEMPLATE_FIX_AND_RETRY, 0, "",
        "Something went wrong on the speaker during that measurement. "
        "Try again.",
    ),
    REASON_RETRIES_SPENT: ReasonSpec(
        REASON_RETRIES_SPENT, TEMPLATE_SESSION_RESTART, 0, "",
        "The retakes for this position are used up. Start another run to measure it again.",
    ),
    **{code: ReasonSpec(code, TEMPLATE_SESSION_RESTART, 0, "", message) for code, message in {
        REASON_LEVEL_UNSOLVED: "No measuring level was found at this position, so this measurement did not play.",
        "placement_required": "Confirm the microphone position before taking another measurement.",
        "retry_gain_missing": "The retake has no test level to use.",
        "take_stopped": "The measurement stopped before the capture was accepted.",
        "cancelled": "The measurement was stopped before it finished.",
        "measurement_door_session_live": "Another measurement is already in progress.",
        "measurement_door_no_volume_owner": "The speaker could not take control of the measurement volume.",
        "measurement_door_volume_not_open": "The speaker could not confirm the measurement volume.",
        "wired_capture_failed": "The microphone could not complete the recording.",
        "program_not_composed": "The speaker could not prepare the test signal.",
    }.items()},
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
        # Names no reflection: a gate window capped at the search ceiling proves
        # nothing about one (gate_disclosure.describe_gate discloses the gate).
        RetryableReasonCopy(
            "The check was inconclusive — this measurement had less usable sound "
            "to compare than the tuning did.",
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
    REASON_ARM_HOST_STUCK: ReasonSpec(
        REASON_ARM_HOST_STUCK, TEMPLATE_HARD_STOP, 0, "",
        "The arm host stopped the measurement because the executor made no progress. "
        "Check the run status and arm trail before starting another measurement.",
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
    REASON_CLOUD_GEOMETRY_LOCKED: _retriable_reason(
        REASON_CLOUD_GEOMETRY_LOCKED, TEMPLATE_FIX_AND_RETRY,
        # RETRIABLE (any non-zero value; see ``ReasonSpec.retry_budget``). The
        # count is the session's own ceiling on wider-spot asks, not what
        # admits the retake: every rung spends one of the POSITION's pooled
        # extras.
        GEOMETRY_RETRY_POSITIONS,
        # The old diagnosis ("too close
        # together") is factually false on a wide walk — the estimator reads
        # only tau, never mic spread, so tau agreement at wide spread is
        # positive evidence FOR a source-fixed defect, not proof the operator
        # huddled. The honest sentence is a finding, not a chore: it names
        # what the dip looks like, not a diagnosis a household cannot judge.
        # The action (a wider-spot retake) is UNCHANGED — the spread-aware
        # skip that would remove it stays deferred pending hardware evidence.
        RetryableReasonCopy(
            "This dip looks like it belongs to the speaker rather than the room.",
            "Take this one from further out and we will use it instead.",
        ),
    ),
    REASON_ANCHOR_TOO_QUIET: _retriable_reason(
        REASON_ANCHOR_TOO_QUIET, TEMPLATE_FIX_AND_RETRY, 1,
        RetryableReasonCopy(
            "JTS heard the speaker, but the test tones were too quiet to line up.",
            LOCATE_RETRY_ACTION,
        ),
        capture_quality=True,
    ),
}


def exception_detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def refusal_copy_for(code: str | None, *, failed_roles: Sequence[str] = ()) -> tuple[str, dict[str, Any] | None]:
    """Household copy and an action; unknown codes use internal-error copy."""
    fallback = REASON_REGISTRY[REASON_INTERNAL_ERROR]
    spec = fallback if code is None else REASON_REGISTRY.get(code, fallback)
    return reason_message(spec.code, spec, failed_roles=failed_roles), dict(spec.next_action) if spec.next_action else None


# The transient codes whose first retry is automatic (a banner, no decision
# screen) per §5.10 template 1.
TRANSIENT_AUTO_RETRY_CODES = frozenset(
    code for code, spec in REASON_REGISTRY.items()
    if spec.template == TEMPLATE_SILENT_AUTO_RETRY
)


def reason_message(
    code: str, spec: ReasonSpec, *, failed_roles: Sequence[str] = (),
) -> str:
    """The household sentence for ``code``, given what the failure recorded.

    THE single copy selector: one failure is narrated on surfaces that never
    see each other — the capture verdict, the envelope, and the apply-seam
    refusal — and a household looking at two of them after ONE failure must
    not be handed two accounts of it. An evidence-keyed code adds its branch
    HERE; a caller that renders ``spec.message`` directly re-opens the gap.

    ``spec`` is passed in rather than looked up so each caller keeps its own
    existence guard.
    """
    if code == REASON_CHANNEL_MAP_MISMATCH:
        return channel_map_mismatch_message(failed_roles)
    # ``or spec.banner`` for the silent-auto-retry codes, whose household
    # text IS the banner and whose ``message`` is empty by construction.
    return spec.message or spec.banner


# Conditions no extra attempt can clear.
NON_RETRIABLE_CODES = frozenset(
    code for code, spec in REASON_REGISTRY.items() if spec.retry_budget == 0
)
CAPTURE_QUALITY_REFUSAL_CODES = frozenset(
    code for code, spec in REASON_REGISTRY.items() if spec.capture_quality
)


TakeNext = Literal["accept", "retake_same", "retake_louder", "retake_quieter", "fix_and_retake", "stop"]
TakeCharge = Literal["speaker", "operator", "none"]


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
    screens: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class PhaseVerdict:
    """A phase verdict: acceptance plus the internal reason (if any)."""

    accepted: bool
    code: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, float | bool | str] = field(default_factory=dict)

    capabilities: dict[str, bool] = field(default_factory=dict)
    next: TakeNext | None = None
    next_gain_db: float | None = None
    charge: TakeCharge = "operator"

    @classmethod
    def from_take(cls, take: TakeVerdict) -> PhaseVerdict:
        return cls(take.ok and take.fault is None and take.next == "accept", take.fault,
                   payload={"screens": take.screens} if take.screens else {},
                   evidence=take.evidence, capabilities=take.capabilities, next=take.next,
                   next_gain_db=take.next_gain_db, charge=take.charge)
