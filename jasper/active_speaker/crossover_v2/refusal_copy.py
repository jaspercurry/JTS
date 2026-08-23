# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What the household is told when a round refuses, and the verdict that says it.

The one sibling here that owns household-facing **copy** rather than a
decision.  (Deliberately not numbered — and since #2291 Phase 5c-v no sibling
is: the ordinals they used to carry were counted on different bases and had
drifted apart.  See this package's ``__init__`` for the rule.)  Everything
else in this package answers with a *kind*
and hands it on; this module owns the other half of that split — the codes
(``REASON_*``), the remediation shapes (``TEMPLATE_*``), the
:class:`ReasonSpec`/:class:`RetryableReasonCopy` carriers, the
:data:`REASON_REGISTRY` that binds a code to its sentence and its retry budget,
the selectors that pick between two sentences for one code, and
:class:`PhaseVerdict`, the value a capture-consuming phase returns.

**Why it is here.**  #2291 Phase 5a-vii ruled that the vocabulary did *not*
have to move, and that ruling was correct for what it was about: an *organ*
answers with a kind, so no organ needed the carrier.  Phase 5c-ii moved it
anyway, on the argument that the surviving spine would land in this package
and would need it.  **That argument did not survive.**  Phase 5c-iv dissolved
the conductor IN PLACE: the spine is
:class:`~jasper.active_speaker.crossover_v2_flow.CrossoverV2Session`, still in
the flow file, so nothing about the spine forces anything about this module.

What actually holds it here is smaller and true: this module binds the
package's OWN refusal kinds to household copy, and it binds ALL of them.
:data:`SCREEN_KIND_REASONS` is keyed by
:data:`~.capture_dispatch.CAPTURE_SCREEN_KINDS` — the union of
:data:`~.spatial.SCREEN_KINDS` (the walked phases' rungs) and
:data:`~.capture_dispatch.ANCHOR_SCREEN_KINDS` (the sit-still phases'),
two disjoint package-owned sets — and covers it exactly, checked in
``tests/test_crossover_v2_spatial.py`` along with the rule that every code
it names is in :data:`REASON_REGISTRY`.  So a new rung in EITHER owner
cannot ship without a household sentence, and the binding sits beside the
kinds it binds.  The flow then imports this module, which is the legal
direction (``test_no_domain_module_imports_the_host_or_the_legacy_flow``
forbids only the reverse).  Legal and coherent; not forced.

**This settles only where the vocabulary *can* live.**  Where it *belongs* is
deliberately still open: the largest single consumer is
:mod:`~jasper.active_speaker.crossover_envelope_v2`, a rendering surface rather
than a deciding one, so "copy sits in the decision package" is a defensible
resting place and not an obvious one.  Tracked as issue #2390; do not read this
module's existence as that question having been answered.

The rule the other siblings state is unchanged and still worth stating: a
*decision* module answers with a kind and never renders a sentence.  What moved
is the ownership pointer — the carrier is no longer "the flow's", it is this
module's.

Dependency direction, as for every module here: no ``jasper.web`` import and
nothing from :mod:`jasper.active_speaker.crossover_v2_flow`.  The flow
re-exports the whole vocabulary below under its historical spellings (all of it
except this module's own ``logger``), so this move changed
no importer anywhere.
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
# Issue #1810 (2026-07-28): the same discriminator W6.12 gave CHECK, for the
# phases CHECK's evidence cannot speak for. MEASURE / cloud / VERIFY each
# carry their own leading pilot pair, and since #1810 their own pre-pilot
# ambient window, so `analysis.pilot_snr_ok` is a real verdict there: False
# means the quiet pilot did not clear the room's own in-band floor by enough
# to trust ANY level comparison drawn from the pair. That is a statement
# about the room and the playback level — a loud room, a mic too far away, or
# (the session that exposed this) a freshly-applied correction that dropped
# the pilot band 14-18 dB and left the quiet pilot ~5 dB over the floor. It is
# NOT evidence about the phone's microphone, which is exactly what the copy
# said before this code existed. `_pilot_observations` reports
# ``linearity_ok`` as None — unknown — whenever the SNR guard fails (it forced
# True until issue #1838, which made an unreadable capture look like a PASS),
# so this branch is the only path that can fail on it, and every verdict below
# checks it BEFORE `REASON_AGC_BEHAVIORAL_FAIL`.
REASON_PILOT_LEVEL_COLLAPSE = "pilot_level_collapse"
REASON_SNR_FLOOR = "snr_floor"
REASON_CHANNEL_MAP_MISMATCH = "channel_map_mismatch"
# The analyzer could not decide WHICH scheduled tone a capture's first arrival
# was, so it cannot say which driver played what. Its whole reason for existing
# is that the alternative is `REASON_CHANNEL_MAP_MISMATCH` — a HARD STOP telling
# a household to open its speaker — decided by a 0.0034 confidence gap on
# 2026-08-16 (issue #2644). Retriable, and its copy names the recording rather
# than the speaker: nothing about the hardware is known to be wrong here, and
# the household must not be handed a rewire instruction the evidence cannot
# support. See `capture_dispatch.SCREEN_ANCHOR_AMBIGUOUS` for the ladder rung.
REASON_ANCHOR_AMBIGUOUS = "anchor_ambiguous"
REASON_CLIPPED = "clipped"
REASON_DRIFT_BASELINES_DISAGREE = "drift_baselines_disagree"
REASON_DELAY_EXCEEDS_SEARCH_WINDOW = "delay_exceeds_search_window"
REASON_LOCATE_FAILED = "locate_failed"
REASON_RELAY_TIMEOUT = "relay_timeout"
REASON_VOLUME_UNRESOLVED = "volume_unresolved"
# The play seam refused/failed the program (safety re-admission over-cap, a
# graph-restore failure, or a session program error) — distinct from a relay
# transport death (``relay_timeout``). After the W6.1 cap-aware composition a
# play-time refusal is unexpected (a bug, a tampered readback, or a genuinely
# infeasible profile), so it is terminal: hard-stop, budget 0.
REASON_PROGRAM_UNPLAYABLE = "program_unplayable"
# R15 (#2106): the program PLAYED — the offline evidence math refused. Design
# §4.2 divides the emitted measurement protection back out of the capture, and
# on a candidate-required bin that division is inadmissible when the protection
# attenuates more than 12 dB or the recovery would exceed 12 dB. Its own code
# exists for the #1820 reason: ``program_unplayable``'s copy claims JTS "could
# not play the measurement signal within the speaker's safe limits", which is
# simply not what happened, and its action (re-check driver details) does not
# reach the lever. Deterministic, so terminal — the same protection and the
# same crossover reproduce it exactly. The offending slug rides out in the
# refusal detail.
REASON_PROTECTION_NOT_SEPARABLE = "protection_not_separable"
# Sibling for the OTHER conditioning branch (panel SF3): `abs(P) < floor` does
# not involve `C`, so "change the crossover frequency" cannot clear it (#1820).
REASON_PROTECTION_SWEEP_TOO_LOW = "protection_sweep_too_low"
# Issue #1820 (2026-07-28): the ONE program refusal that is neither unexpected
# nor about levels, split back out of ``program_unplayable``'s collapse. It is a
# deterministic, one-edit-away state, not a level ceiling the speaker could not
# meet. Collapsed into ``program_unplayable`` it inherited that code's copy
# ("Re-check the driver details in speaker setup"), which is the one action that
# makes it WORSE. Its own code exists so the copy can name the actual exit and
# its ``next_action`` can point at it. Terminal (hard-stop, budget 0) for the
# same reason it is deterministic — a second identical measurement reproduces it
# exactly.
#
# The states that reach it are ``stale`` and ``malformed``; the separate confirm
# ceremony that used to add ``unconfirmed`` to that list is retired (saving the
# declaration IS declaring it), which is why the copy names an ordinary save.
# The SLUG keeps its wire name: it is a stable identifier that ships in
# ``state["failure"]``, the phone envelope, and the journal, and "not confirmed"
# still reads true of every state that reaches it.
REASON_PROGRAM_PROFILE_NOT_CONFIRMED = "program_profile_not_confirmed"
# Its two siblings, added in the same issue's review round. "Review the limits
# and save them again" is only the honest action when there ARE visible limits
# and a save would change the verdict. Two profile states fail that, and the
# session-open pre-flight can tell them apart because it holds the full
# ``DriverSafetyProfileEvaluation``:
#
#   * ``missing``    — no profile exists at all (never-saved / unreadable /
#                      pre-crossover draft). ``/sound/`` deliberately renders NO
#                      safety callout in this state, so telling the household to
#                      review the limits names a panel that is not on the page.
#   * ``incomplete`` — declared values are still missing or do not line up. A
#                      save is allowed but rebuilds the same ``incomplete``
#                      profile, so "save again" would be a circle.
#
# These have no ``ProgramAdmissionRefusal`` counterpart — the play-seam
# vocabulary carries one ``PROFILE_NOT_CONFIRMED`` slug for all three — so they
# are reachable only from the pre-flight, which is the point: the gate that has
# the evidence is the gate that names the action.
REASON_PROGRAM_PROFILE_MISSING = "program_profile_missing"
REASON_PROGRAM_PROFILE_INCOMPLETE = "program_profile_incomplete"
# Any OTHER host-side fault the session runner's catch-all cleanup arm caught
# (W6.1 gate: the seams raise open-endedly — CamillaUnavailable is a bare
# Exception, analyze/emit raise ValueError/RuntimeError, the held measurement
# window raises MeasurementWindowError — so an enumerated except list is how
# failures escape with the volume active and the phone frozen). Terminal for
# the session; the household's one action is to try again.
REASON_INTERNAL_ERROR = "internal_error"
REASON_VERIFY_OUT_OF_TOLERANCE = "verify_out_of_tolerance"
# Issue #1873 (owner field report, 2026-07-29): the SAME out-of-tolerance
# observation, once a second graded attempt has shown it REPEATS. The code above
# is honest about one attempt and dishonest about two: it is retriable, and its
# copy invites a retry, because a single mismatch really can be a bad take. When
# consecutive attempts agree inside the instrument's own repeat floor (the
# session that filed this measured 3.66 dB then 3.82 dB against a 1.5 dB
# tolerance — 0.16 dB apart), the mismatch is a FINDING about the speaker, and
# every further retry re-measures the same applied graph into the same answer
# while the relay session's clock runs out. In the owner's words: "The speaker
# didn't match the prediction — that's just the reality of what it is. This
# shouldn't be 'you don't like it, give me something you do like.'"
#
# Its own code rather than a second sentence on the code above, for the reason
# every split in this file has: the two states differ in what the household
# should DO. Terminal (budget 0, so ``NON_RETRIABLE_CODES``) because a retry
# cannot change it — the same "deterministic ⇒ terminal" rule
# ``program_profile_not_confirmed`` and ``protection_not_separable`` already
# state. Renders through the SAME ``verify_fail`` template as its four
# siblings; it is one more parameterization of that screen, not a new screen.
REASON_VERIFY_DETERMINISTIC_MISMATCH = "verify_deterministic_mismatch"
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
# R18 / #1868: the applied result tracks the model but does NOT meet the
# candidate's own crossover target through the handoff — the case
# ``REASON_VERIFY_OUT_OF_TOLERANCE`` structurally cannot catch, since it grades
# measured-vs-model and a defect present in BOTH sides cancels. Its own code
# because the household's situation differs: the graph did what it was told,
# and the crossover as designed-and-aligned is what does not sum.
REASON_VERIFY_CROSSOVER_REGION = "verify_crossover_region"
# Owner ruling (2026-07-20): the alignment-estimator confidence floor that
# used to gate ONLY a review-screen nudge (informed consent, Apply stayed
# available regardless) is now a hard MEASURE-phase gate — see
# ALIGNMENT_CONFIDENCE_TRUST_FLOOR below. A household has no basis to judge a
# raw confidence number, so doubt becomes guidance ("move the mic"), never a
# question ("apply anyway?").
REASON_LOW_ALIGNMENT_CONFIDENCE = "low_alignment_confidence"
# The apply transaction came back blocked or raised. It was the session's
# OWN auto-apply until the two-stage split (D1); since then the only apply is
# the household's POST from the review screen, which persists its blocking
# issue through ``_persist_apply_blocked`` and answers the request directly.
# The code is retained: it is still the honest name for "the apply failed",
# and ``_persist_terminal_failure`` still scopes its §5.6 evidence reset away
# from it (an apply failure says nothing about the mic position).
REASON_APPLY_FAILED = "apply_failed"
# A deliberate phone Stop (CaptureAborted, abort_reason == "stopped") is not a
# relay-transport death — see the catch-all's exception classification in
# jasper.web.correction_crossover_v2. Reuses TEMPLATE_SESSION_RESTART's
# rendering shape (a fresh session is the only way forward either way) with
# honest copy instead of a manufactured "timed out" claim.
REASON_USER_STOPPED = "user_stopped"
# The deferred apply/"review" hold (CaptureBeginDeferred "awaiting_apply")
# expired before an apply completed. Distinct from a relay-transport death
# (relay_timeout) and a deliberate phone Stop (user_stopped): name the actual
# cause rather than a generic "the measurement link timed out" claim (#1605).
# Same TEMPLATE_SESSION_RESTART shape — a fresh session is the only way
# forward. RETAINED but unreached since the two-stage split (D10): no shipped
# session holds for an apply any more.
REASON_REVIEW_HOLD_TIMEOUT = "review_hold_timeout"
# The position gate's three refusals, reachable by EITHER gated shape (#2879):
# the externally positioned tier (``TIER_REMOTE``), whose driver reports each
# arrival, and a hand-walked round on the WIRED capture source, where the
# person holding the tape does. Named reasons rather than a fall-through to
# ``relay_timeout`` because the position gate is consulted AHEAD of the
# conductor — nothing sets ``last_failure_code`` on this path, so an unnamed
# gate refusal persisted as "the measurement link timed out", which is a claim
# about the transport that no transport made. The copy below therefore names
# neither mover: one sentence has to read true to an operator watching an arm
# and to a household member holding the microphone.
#
#   position_hold_expired  — nothing reported the microphone in place before
#                            REMOTE_POSITION_HOLD_BUDGET_S. The session is over;
#                            a fresh one is the only way forward.
#   position_target_missing— a plan entry carried no target angle, so the gate
#                            refused rather than measure an unknown position.
#                            A build-shape disagreement, not an operator error.
#   session_ceiling_expired— the WHOLE walk outlived the session's wall-clock
#                            ceiling while a hold was pending, with no single
#                            hold having expired (issue #2506). The per-hold
#                            budget catches a driver that STOPS; this catches
#                            one that is merely slow at every position, and it
#                            is a different sentence: nothing stalled, the walk
#                            ran out of measurement window. Without it that
#                            death limped on to the relay link's own expiry and
#                            reached the household as ``relay_timeout`` — the
#                            same transport claim about a healthy transport that
#                            the two codes above exist to avoid.
#
# All three TEMPLATE_SESSION_RESTART, for the same reason the review hold is: no
# retry at this position can help once the session has been torn down.
REASON_POSITION_HOLD_EXPIRED = "position_hold_expired"
REASON_POSITION_TARGET_MISSING = "position_target_missing"
REASON_SESSION_CEILING_EXPIRED = "session_ceiling_expired"
# The geometry-locked retake asks for a pose PAST the walk — 75 cm out, and on
# its second rung 75 cm out AND above mark height. NO GATED session can serve
# it, and for two reasons rather than one (#2879): an external positioner
# swings on one horizontal axis at a fixed radius, so it can reach neither
# rung; and the retry re-authorizes the same plan entry, so the position gate
# goes on naming that entry's original BEARING while the screen names the wider
# spot — two answers to where the microphone should be, which is a person's
# problem even though a person could walk there. Rather than prompt for a move
# that cannot be made, or made honestly, a gated session refuses here and
# recommends the screen-paced instrument that CAN ask for it.
REASON_GEOMETRY_RETAKE_UNREACHABLE = "geometry_retake_unreachable"
# Position-group choreography (flat-linearization PR-3b): the pre-apply cloud
# closed with `spatial_combine.assess_geometry` reporting `locked` — every
# position's echo estimate landed on the same tau, so the nulls are not moving
# and spatial averaging cannot fill them. NOT a bad capture: the capture is
# fine and the operator did nothing wrong. It is the one actionable thing the
# geometry instrument can say ("spread the mic further"), so the group asks for
# that position again from a wider spot, at most ``GEOMETRY_RETRY_POSITIONS``
# times, and then proceeds with the verdict RECORDED (journal + durable
# state; PR-4 carries it on the envelope and `/state` — no household-facing
# surface renders it yet, PR-7 renders it) rather than blocking a
# measurement on a defect no mic move can decorrelate.
REASON_CLOUD_GEOMETRY_LOCKED = "cloud_geometry_locked"
# Accountability assertions (linearization-integrity PR-L4). Both refuse a
# candidate at the confirm seam, so no proposal ever reaches the review screen
# and the speaker is never touched: the honest outcome of "we cannot show this
# makes your speaker better" is to leave it alone and say so.
#
# item 1 — the two drivers' realized levels, read on their own mirrored
# ±1-octave half-bands about Fc after the committed trim, sit further apart than
# REALIZED_LEVEL_MATCH_TOLERANCE_DB. A 2-way sums flat only when both branches
# hand off at the same level, so this is a tonal-balance defect that no amount
# of per-driver flattening can hide. Fired at ~9 dB on the 2026-07-27 JTS3
# profile the owner heard as dark. (It grades the HANDOFF, not the whole
# passband: a driver whose own band tilts while its half-band level is right is
# the fit's problem to catch, not this assertion's.)
REASON_DRIVER_LEVELS_DISAGREE = "driver_levels_disagree"
# item 2 has NO reason code. It used to have two — ``correction_not_an_improvement``
# and its prescribed-class sibling, one refusal apiece — and the nanny burn-down
# (docs/measurement-loop-doctrine.md deviation (c)) deleted both with the refusal
# they existed to name. Item 2 now banks
# ``accountability.LEDGER_NOT_AN_IMPROVEMENT`` and the round proceeds to the
# measurement that decides, so there is no household sentence left to write:
# nobody is being told their speaker was left alone. A durable state persisted
# before that change can still carry either literal, and every reader of one
# already tolerates a code with no registry row — ``_failure_history_note`` in
# ``crossover_envelope_v2`` reads the registry with ``.get`` and falls back to
# its generic clause.
#
# Delta-probe verdicts (linearization-integrity PR-L5). Unlike item 1 above,
# these fire AFTER the apply — they are what the post-apply sweep found — so
# each one rolls the correction back before it names itself. The household is
# left on the sound they had, and told why, which is the difference between an
# automatic rollback and a silent one.
#
# The correction did not do what its own filters said it would: a chain defect
# (the shelf realized at a Q the fit never modelled is the archetype, and the
# reason this code exists permanently rather than as a one-off fix).
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
# is therefore STILL APPLIED, and the copy has to say so — the household is
# listening to it right now, and telling them it was put back would be a false
# statement about their speaker. Mirrors the room-correction acceptance
# precedent: a failed automatic restore must continue to say the correction is
# still applied, and name the manual action.
REASON_CORRECTION_ROLLBACK_FAILED = "correction_rollback_failed"
# #2291's round verdict, and the one cause no code above can carry: the
# correction was applied, MEASURED at the same mark with the same program, and
# the speaker is measurably worse than it was before — so it came back off.
# This is what item 2's deleted refusal used to forecast, and the difference is
# the whole burn-down: that one graded a PREDICTED response before the apply and
# refused on it, where this one is a measurement of the applied graph. Also
# distinct from the three delta-probe codes, which say the graph did not do
# what its own filters commanded. Here the graph did exactly what it was told
# and the room liked it less, which is a different sentence to the household
# and a different next step.
REASON_CORRECTION_MEASURED_REGRESSION = "correction_measured_regression"
# #2291's fail-closed boost. The benefit could not be measured — no comparable
# "before", or a capture that could not be compared — and the applied
# intervention puts energy INTO a driver. An unverified cut can wait for a
# household to decide; an unverified boost cannot, so it comes off. Its own
# code because "we could not tell, and erred toward your drivers" is a
# different and more honest sentence than "it measured worse".
REASON_CORRECTION_UNPROVEN_BOOST = "correction_unproven_boost"
# #2537's safety row. The post-apply sweep measured the applied graph putting
# out MORE than it declared — a commanded boost realized above its bound, an
# uncommanded level shift in the LOUD direction, or a capture that clipped — so
# it came off. Its own code because the three siblings above all say something
# else: the graph did what it was told and the room liked it less
# (measured_regression), the graph missed its own shape (model_error), or
# nothing could be measured (unproven_boost). Here the graph was measured doing
# MORE, which is the only cause on this list that is about output rather than
# accuracy, and the only one a household could be hearing right now.
#
# ONE row rather than three hazard-specific ones, on
# :data:`REASON_CORRECTION_ROLLBACK_FAILED`'s precedent directly below: the
# household's first fact is that a measured change was found unsafe and put
# back, the action is identical in all three cases, and the specific hazard is
# on the round's own record (the safety verdict's reason, in the receipt's
# ``round_axes`` and the journal) for whoever needs it afterwards.
REASON_CORRECTION_UNSAFE_RESULT = "correction_unsafe_result"
# #2537's untrusted row, for an intervention that puts no energy in. Its
# boosted sibling is REASON_CORRECTION_UNPROVEN_BOOST, whose copy leans on "and
# it turns some parts up" — a sentence that is false for a cut-only correction,
# which is why this exists rather than reusing it. The finding is the same and
# the remedy is the same; what differs is the reason the round erred toward
# putting the old sound back.
REASON_CORRECTION_UNVERIFIABLE_RESULT = "correction_unverifiable_result"

def round_restore_reason(cause: str) -> str:
    """#2537 adoption cause → the code a SUCCESSFUL round restore surfaces.

    Every cause the table can reach with a ``restore`` outcome is here, and
    each maps to the code whose copy states that cause truthfully:

    * the three SAFETY causes share
      :data:`REASON_CORRECTION_UNSAFE_RESULT` — see its note for why one row;
    * the four EVIDENCE-TRUST causes share
      :data:`REASON_CORRECTION_UNVERIFIABLE_RESULT`, unless the applied
      intervention was boosted, in which case ``decide_adoption`` has already
      substituted :data:`~...verification.ADOPTION_UNPROVEN_BOOST` as the cause
      and its own stronger sentence renders;
    * a measured regression keeps its own.

    ``realization_failed`` is deliberately absent since #2537: it is a QUALITY
    cause now, and quality's only restoring value is a measured regression, so
    a realization failure can no longer reach this function. Leaving its entry
    would be a row for a state nothing produces.

    A function with a lazy import rather than a module-level dict, because
    :mod:`~jasper.active_speaker.crossover_v2.verification` reaches
    :mod:`~jasper.active_speaker.flat_spec`, which this module imports lazily
    everywhere for that reason.

    **A delta-probe rollback class keeps the probe's OWN sentence.** Since the
    fifth-principle routing, the three classes that used to restore from the
    probe's own seam reach this function instead, carrying their verdict in a
    composite cause (``delta_probe_rollback_class:<verdict>``). Each still
    renders the code it always did, through the one mapping that owns that
    correspondence (:data:`DELTA_PROBE_REASON_BY_VERDICT`) — a household whose
    speaker was reverted for a shape mismatch must not start reading the
    generic unverifiable sentence because the DECISION moved one module over.

    Anything unlisted falls back to the unverifiable code — the weakest true
    statement available for "the round asked for a restore", and a safer floor
    than the measured-regression code it used to be: claiming a REGRESSION the
    round did not find is a false statement about a household's speaker, while
    claiming the result could not be verified is true of every unmapped cause
    by construction. The mapping is exhaustive today and pinned by a test, so
    the fallback is a floor, not a branch anything reaches.
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
        # The probe's own per-class sentence, from its one owner. An unknown
        # class falls through to the same floor as any other unlisted cause
        # rather than to a guessed one.
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
#: over :data:`delta_probe.DELTA_PROBE_ROLLBACK_VERDICTS`, pinned by a test.
#:
#: The stated intent has always been "a new NON-MATCHED verdict cannot ship
#: without a surface", and until #1811 the rollback set and the non-matched set
#: were the same thing, so equality here enforced it. ``level_mismatch`` is the
#: first verdict that is non-matched WITHOUT being a rollback, so the two sets
#: diverged and this mapping alone stopped covering the intent. The guard test
#: is now written against the non-matched set: a verdict that is not here must
#: prove it reaches a household some OTHER way (``level_mismatch`` does — the
#: persisted ``verify.delta_probe`` summary and the done screen's caveat
#: nudge), never merely by being absent from a rollback list.
DELTA_PROBE_REASON_BY_VERDICT: Mapping[str, str] = {
    VERDICT_MODEL_ERROR: REASON_CORRECTION_MODEL_ERROR,
    VERDICT_LEVEL_DEPENDENT_SHORTFALL: REASON_CORRECTION_LEVEL_SHORTFALL,
    VERDICT_SPATIALLY_COSTLY: REASON_CORRECTION_SPATIALLY_COSTLY,
}


def verify_inconclusive_cause(
    code: str | None, reflection_measured: bool | None,
) -> str:
    """WHY a verify check could not settle, as one household clause (#1974).

    **THE single writer of that clause**, because it renders on TWO screens —
    the verify_fail screen's reason copy and the done screen's ungraded
    verdict — and those two screens is exactly how the bug this fixes stayed
    invisible: each carried its own paraphrase of "the room reflection cut the
    window short", so neither could be corrected without the other being
    noticed. There is now one sentence and two framings of it.

    Two things produce the "inconclusive" outcome and they share no mechanism:

    * ``REASON_VERIFY_INCONCLUSIVE`` — VERIFY's own gate came out SHORTER than
      MEASURE's, so the two captures cannot be compared like for like. That is
      the whole of what the rule observed; WHY the window is short is a
      separate fact, and it is the one the old copy asserted without ever
      consulting. ``reflection_measured`` is that fact, taken from
      :attr:`~jasper.audio_measurement.gate_disclosure.GateDisclosure.gated_anything`
      — the single owner of "is the reflections claim true here", whose own
      docstring says it is true THERE and nowhere else. Across the whole
      2026-07-30 corpus it was False (issue #1966), i.e. the sentence people
      actually read was false on every capture that produced it.
    * ``REASON_VERIFY_LEVEL_SHIFT`` — the recording chain moved between
      attempts. No reflection and no window are involved at all, and this path
      never reaches the verify_fail screen's inconclusive copy (it has its own
      ReasonSpec); it reaches the DONE screen's, because that screen keys on
      the coarse outcome rather than the code.

    The two arguments go unknown for different reasons and get different
    answers, and the difference is load-bearing:

    * ``code=None`` — the record does not say WHICH verdict fired (a durable
      state written before this shipped). Nothing at all is established, so
      the clause is EMPTY: the caller states the outcome and stops, which is
      the honest rendering of an unrecorded cause.
    * ``reflection_measured=None`` — the verdict IS known, only its gate is
      not. That collapses into the no-reflection-claim branch below rather
      than emptying the clause, because the code alone already establishes the
      observation ("the window came out shorter than the tuning's") — that is
      what the rule measured, independent of any gate record. Emptying here
      would also break :func:`verify_inconclusive_message`, whose registry
      rendering passes exactly this and would otherwise read "The check was
      inconclusive — . Re-verify to try again."

    Returned without terminal punctuation: the caller owns the sentence it
    lands in.
    """
    if code == REASON_VERIFY_LEVEL_SHIFT:
        # Same vocabulary as REASON_VERIFY_LEVEL_SHIFT's own ReasonSpec below,
        # deliberately: one cause should not have two names depending on which
        # screen a household happens to be reading.
        return "the microphone's levels changed between measurements"
    if code != REASON_VERIFY_INCONCLUSIVE:
        return ""
    if reflection_measured:
        # The ONE state where blaming a reflection is true — and it says what
        # the comparison actually lost, not merely that a reflection existed.
        return (
            "a reflection reached the microphone sooner than it did during "
            "tuning, so there was less of the sound to compare"
        )
    # Reflection NOT measured, or not recorded. Both render the observation the
    # rule made and stop there: a window capped at the search ceiling proves
    # nothing about reflections, so naming one would be the same overstatement
    # in a new place. The precise gate state is disclosed a line below in
    # expert details, by ``gate_disclosure.describe_gate``.
    return "this measurement had less usable sound to compare than the tuning did"


def verify_inconclusive_diagnosis(reflection_measured: bool | None) -> str:
    """What VERIFY established, without advice about the next action."""
    cause = verify_inconclusive_cause(REASON_VERIFY_INCONCLUSIVE, reflection_measured)
    return f"The check was inconclusive — {cause}."


def verify_inconclusive_message(reflection_measured: bool | None) -> str:
    """``REASON_VERIFY_INCONCLUSIVE``'s household sentence. Single writer.

    The registry entry below holds this function's ``None`` (cause-unknown)
    rendering, so a caller with no gate record on hand — and every reader of
    ``REASON_REGISTRY`` — gets copy that is true rather than copy that guesses.
    The envelope re-renders it with the persisted fact when it has one.
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
    """``REASON_LOCATE_FAILED``'s household sentence. Single writer (#2085).

    SELECTION, never composition — the same shape
    :func:`verify_inconclusive_message` above uses, and for the same reason:
    one code, two honest causes, and a registry that cannot hold one literal
    true of both.

    ``locate_failed`` fires when the correlator could not place this capture's
    stimuli (:func:`_stimulus_locate_ok`, :func:`_sweep_locate_confidence_ok`,
    or VERIFY's ``summed_sweep_heard`` integrity check — all three are
    locate-CONFIDENCE floors). Its copy asserted the one cause that would
    explain that on its own: the speaker was not audible, so check the volume
    and the microphone. The JTS3 session of 2026-08-03 measured that claim
    false three times in one sitting. Every one of those captures carried
    ``pilot_snr_ok=True`` — the leading pilot pair cleared the room's own
    in-band floor by 13.9-15.5 dB, direct evidence from THIS capture that the
    speaker was heard — while its sweeps scored 0.019-0.097 against a 0.3
    floor. A household told to check the volume then goes and changes the one
    thing the measurement had already proved was fine.

    **The copy names the operation that failed, and stops there.** Forensics
    on those same three WAVs found the audio pristine: the analyzer had
    anchored the timeline on ``pilot_lo`` — deliberately the quietest segment
    in the program — missed the anchor gate by an NCC margin of 0.005-0.049,
    snapped to ``pilot_hi`` instead, and put every subsequent sweep 1296.5 ms
    (exactly the pilot spacing) outside a +/-30 ms search window. Re-scored
    with a whole-capture search the same recordings give 0.67-0.82. So "the
    recording came back damaged" would have been a THIRD false sentence, told
    to households whose volume AND whose recording were both fine. What is
    true in every case — a corrupted capture and this mis-anchor alike — is
    that JTS could not line up the test tones. That is what the household is
    told. (The anchor itself is a separate fix in ``program_analysis``; this
    copy does not depend on it landing, and does not become wrong when it
    does.)

    ``pilot_heard`` is the discriminator:

    * ``True`` — the pilot pair was measurably heard, so "couldn't hear the
      speaker" is refuted BY THIS CAPTURE. The copy reports the lining-up
      failure and asks for one retry, asserting no cause for it.
    * ``False`` / ``None`` — the pilot failed too, or there is no pilot
      evidence at all. Then the level/microphone reading is either supported
      or simply unknown, and the original copy stands. The registry holds
      this rendering, so every reader of ``REASON_REGISTRY`` with no capture
      in hand gets copy that is true rather than copy that guesses.

    Deliberately keyed on the EVIDENCE, not on which gate fired. The three
    call sites above measure the same thing (a locate-confidence floor) and
    the falsifying fact is the same field on the same analysis, so keying on
    the site would let one measured situation produce two different sentences
    depending on which floor happened to be checked first — the drift this
    file already fixed once for the inconclusive copy (#1974).
    """
    diagnosis = locate_failed_diagnosis(pilot_heard)
    if pilot_heard:
        return f"{diagnosis} Try again."
    return f"{diagnosis} Check the volume and the microphone, then try again."


@dataclass(frozen=True)
class RetryableReasonCopy:
    """One retryable reason's diagnosis and still-available action.

    ``diagnosis`` is the observation that remains true after the slot's last
    extra attempt.  ``retry_action`` is appended only on surfaces where an
    attempt is still available.  Keeping both pieces in this one value lets
    :class:`ReasonSpec` expose the historical full ``message``/``banner``
    strings without duplicating the diagnosis in a terminal-copy registry.

    ``strip_before_join`` supports the two existing em-dash sentences: their
    standalone diagnosis ends with a period, while the retryable rendering
    removes that period before the dash.  The diagnosis itself remains a
    complete household sentence.
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
    # RETRIABLE-OR-NOT, since the bounded-retry ruling (#2086) moved the COUNT
    # to :data:`MAX_EXTRA_ATTEMPTS_PER_POSITION`. Zero still means "no extra
    # attempt can help" — a statement about the CONDITION (wiring is wrong, the
    # tuning would not have improved the speaker), not a budget — and those
    # codes still stop the moment they fire. Any non-zero value now says only
    # "retriable"; the specific 1 vs 2 no longer changes behaviour, because a
    # per-code count was exactly the fragmentation the ruling replaced (five
    # attempts at one position on 2026-08-03 came from three codes each holding
    # its own meter). Kept as an int rather than collapsed to a bool to keep
    # this change off every registry entry's line; see
    # :data:`NON_RETRIABLE_CODES`.
    retry_budget: int
    # Short banner shown while a transient code auto-retries (template 1). Empty
    # for codes whose template is a decision screen.
    banner: str
    # The fix/action copy the decision-screen template renders. One reason, one
    # action (the Language guide).
    message: str
    # Optional per-reason override for the HARD-STOP screen's action button
    # (issue #1820). Consulted by that template ONLY, because it is the one
    # screen whose default action is a generic destination ("Back to speaker
    # setup", ``/sound/``) rather than a semantically load-bearing control —
    # verify_fail owns Undo, session_restart owns Start over, fix_and_retry
    # owns Try again, and none of those may be replaced by copy data. A
    # hard-stop reason that knows the exact control which clears it declares
    # that control here so the household lands ON it instead of on the page
    # that contains it. Shape is the ``next_action`` mapping the envelope
    # emits: ``{"id", "label", "href"}``.
    next_action: Mapping[str, Any] | None = None
    # Structured only for retryable rows.  ``message``/``banner`` above is
    # derived from this value by :func:`_retriable_reason`, so the diagnosis
    # used at exhaustion and the diagnosis inside retry copy have one writer.
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
        # Copy amended 2026-07-28 (issue #1810). It used to state the cause
        # outright — "Your phone's microphone changed its own levels
        # mid-measurement" — and the JTS3 session that filed the issue proved
        # that claim can be false: the pilot pair had collapsed into the room
        # floor, the only direct recording-chain evidence path
        # (``pilot_transfer_step_db``) was null, and the household was told to
        # go re-allow a microphone that had done nothing wrong. What this code
        # actually observes is that the captured two-pilot level delta did not
        # match the programmed one at a level where it should have. Two things
        # produce that — the phone's input chain riding gain, or the speaker's
        # own output compressing — so the copy names the observation and the
        # one action that helps either way. The definite mic accusation now
        # lives ONLY on REASON_VERIFY_LEVEL_SHIFT, which has the cross-attempt
        # transfer step to back it.
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
        # One reason, one action (the Language guide) — but the cause is
        # genuinely two-sided and naming only half of it would be the same
        # over-claim this code exists to stop. "Not your phone" is the point:
        # the household's previous experience of this failure was being told
        # to re-allow a microphone that was working.
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
        # Fix 3 (W6.4): with Fix 1's band-relative discriminator this should
        # be rare and genuinely wiring, but the honest failure mode also
        # includes a very quiet/noisy room, so this copy names both causes
        # rather than blaming wiring unconditionally. Both causes are still
        # live after the 2026-08-21 switch from an additive cross-rise bound to
        # the ISOLATION RATIO (`program_analysis.CHANNEL_MAP_MIN_ISOLATION_DB`,
        # `target_rise - cross_rise`): a room loud enough to bury a driver
        # fails the TARGET floor and arrives here, and room energy landing in
        # the other driver's band still eats isolation. What changed is which
        # of the two is likelier — an honest capture's cross energy is skirt
        # content at a fixed RELATIVE level, so a louder session no longer
        # manufactures this refusal, and a room that reaches it now does so by
        # burying the driver rather than by out-shouting a flat 6 dB bound.
        # The numbers behind a given refusal are on
        # `event=correction.crossover_v2_check_diag`, which publishes each
        # role's raw rises, its isolation ratio, and the bound that ratio was
        # graded against; this household-facing copy stays number-free (the
        # Language guide: one reason, one action).
        "The drivers didn't play in the expected order — check the speaker "
        "wiring, or if the room is noisy, quiet it and try again.",
    ),
    REASON_ANCHOR_AMBIGUOUS: _retriable_reason(
        REASON_ANCHOR_AMBIGUOUS, TEMPLATE_FIX_AND_RETRY, 1,
        # One reason, one action (the Language guide). The diagnosis is
        # deliberately about the RECORDING and not about the speaker: this code
        # fires precisely when the evidence does not identify which driver
        # played what, so naming any cause in the speaker would be the
        # over-claim the code exists to prevent. It does not ask the household
        # to quiet the room either — the 2026-08-16 capture that produced it had
        # BETTER pilot SNR than the round that passed, so "the room was loud"
        # would have been false. Re-recording is what actually clears it,
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
        # NOT a literal (issue #2085). This code is a locate-CONFIDENCE floor,
        # and its copy named the one cause that would explain a miss on its own
        # — an inaudible speaker — on captures whose own pilot pair proved the
        # speaker was heard. The sentence has one writer now
        # (``locate_failed_message``, which also explains why the heard-speaker
        # branch names no cause at all); what the registry holds is its
        # no-pilot-evidence rendering, true for any reader with no capture in
        # hand. The relay verdict and the envelope both re-render it with the
        # measured fact.
        RetryableReasonCopy(
            locate_failed_diagnosis(None),
            "Check the volume and the microphone, then try again.",
        ),
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
        # Issue #1820 defect 2: the copy this refusal used to inherit from
        # ``program_unplayable`` sent the household to "re-check the driver
        # details", which was the one action that made it worse. This copy names
        # the actual exit and the ``next_action`` below lands ON the explanation
        # rather than on the page that hides it behind a disclosure.
        #
        # The states that reach here are ``stale`` (the outputs moved underneath
        # the saved limits) and ``malformed`` (JTS cannot read them back). Both
        # end the same way: open the limits and save them again. There is no
        # separate confirm step any more — saving the declaration IS declaring
        # it — so the copy no longer names one, and an ordinary edit no longer
        # lands the household here at all.
        "JTS could not use this speaker's saved safety limits, so it did not "
        "play the measurement signal. Review the limits in speaker setup and "
        "save them again, then measure.",
        next_action={
            "id": "review_safety_limits",
            "label": "Review safety limits",
            # ``/sound/``'s Component setup card renders the hoisted review
            # callout under this exact id whenever the limits are unusable
            # (deploy/assets/sound-profile/js/main.js), and its boot path opens
            # the owning step for this fragment. Both halves are pinned by
            # tests/test_sound_profile_confirm_deeplink.py.
            "href": "/sound/setup/#confirm-safety-limits",
        },
    ),
    REASON_PROGRAM_PROFILE_MISSING: ReasonSpec(
        REASON_PROGRAM_PROFILE_MISSING, TEMPLATE_HARD_STOP, 0, "",
        # NOT "review the safety limits": there are none to review and no
        # callout naming them. This is the state the pre-gate's original copy
        # was right about, kept for exactly this branch.
        "This speaker's driver details are not finished, so JTS has no safety "
        "limits to measure within. Finish the driver details in speaker setup, "
        "then measure again.",
        next_action={
            "id": "speaker_setup",
            "label": "Finish speaker setup",
            # No fragment: ``/sound/`` renders no review callout in this state,
            # so a deep link would land on nothing. The page opens on its own
            # first unfinished step, which IS the action.
            "href": "/sound/setup/",
        },
    ),
    REASON_PROGRAM_PROFILE_INCOMPLETE: ReasonSpec(
        REASON_PROGRAM_PROFILE_INCOMPLETE, TEMPLATE_HARD_STOP, 0, "",
        # Matches what ``/sound/``'s own callout says in this state — the two
        # surfaces name one action, and it is adding the values, not saving:
        # a save with values missing rebuilds an ``incomplete`` profile again.
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
            "Try again, or undo to restore the previous sound.",
        ),
    ),
    # #1873. Budget 0 — the ONE verify_fail row that is not retriable, and the
    # reason it is not is the reason it exists: a second graded attempt already
    # agreed with the first inside the instrument's repeat floor, so a third
    # lands in the same place. The copy says so rather than leaving the
    # household to discover it by spending the session's remaining time.
    #
    # Order of the sentence is the order the household needs it: the finding
    # first (this is what your speaker does), then WHY the obvious button is
    # gone, then the two levers that can actually change the outcome. Both are
    # already on this screen — ``_verify_fail_envelope``'s Undo and Re-measure —
    # and for a non-retriable code that envelope promotes Re-measure to the
    # primary rather than offering a "Try again" this row has just ruled out.
    # Naming two actions follows ``verify_crossover_region``'s precedent: when
    # neither lever dominates, listing one would be picking for the household.
    REASON_VERIFY_DETERMINISTIC_MISMATCH: ReasonSpec(
        REASON_VERIFY_DETERMINISTIC_MISMATCH, TEMPLATE_VERIFY_FAIL, 0, "",
        "JTS checked twice and measured the same difference both times, so "
        "this is what your speaker actually does — not a bad measurement, and "
        "another try lands in the same place. Undo to restore the previous "
        "sound, or re-measure to fit the crossover again.",
    ),
    REASON_VERIFY_CROSSOVER_REGION: _retriable_reason(
        REASON_VERIFY_CROSSOVER_REGION, TEMPLATE_VERIFY_FAIL, 2,
        # Says what was measured, no diagnosis — a handoff dip can be
        # alignment, spacing, Fc, or the horn, and this cannot tell them apart.
        # The hint deliberately does NOT lead with "try again": a retry
        # re-checks the SAME applied graph and this defect is deterministic, so
        # that is a near-dead lever. It names the two that change the outcome.
        RetryableReasonCopy(
            "The two drivers didn't blend as designed where they hand over.",
            "Re-measure to fit it again, or undo to restore the previous sound.",
        ),
    ),
    REASON_VERIFY_INCONCLUSIVE: _retriable_reason(
        REASON_VERIFY_INCONCLUSIVE, TEMPLATE_VERIFY_FAIL, 2,
        # NOT a literal (issue #1974). This copy used to assert "the room
        # reflection cut the window short" on a verdict that never consulted
        # whether a reflection was found — and across the whole 2026-07-30
        # corpus none was. The sentence has one writer now
        # (``verify_inconclusive_message``), and what the registry holds is its
        # cause-unknown rendering: true for any reader with no gate record.
        # The envelope re-renders it with the persisted fact.
        RetryableReasonCopy(
            verify_inconclusive_diagnosis(None),
            "Re-verify to try again.",
        ),
    ),
    REASON_VERIFY_LEVEL_SHIFT: _retriable_reason(
        REASON_VERIFY_LEVEL_SHIFT, TEMPLATE_VERIFY_FAIL, 2,
        # The instrument is named device-agnostically (#1941 R4): the session
        # mic may be a UMIK-2 or a laptop, and #1924's field evidence is a
        # UMIK-2 session told its phone had drifted.
        #
        # ROUTING (#1924, the half R4 deferred). ONE string renders on TWO
        # surfaces where "try again" is a DIFFERENT control, so the copy has to
        # be true on both without discrediting either:
        #
        # * measurement page (``renderPlanRetry``) — the in-session re-arm,
        #   which re-compares against the SAME reference this attempt just
        #   failed against. A level that moved and stayed moved repeats here
        #   until the budget dies.
        # * wizard (``_verify_fail_envelope``) — a FRESH relay session, which
        #   since #1927 builds a fresh session and re-baselines, so this gate
        #   is structurally unreachable on its first attempt. Retry settles it
        #   in one capture.
        #
        # The old ending ("re-verify to try again") commanded the retry, which
        # is the phone's dead end. Naming only Re-measure/Undo would have been
        # the mirror-image error: it discredits a wizard button that works, and
        # the screen's visible primary IS "Try again". So the sentence states
        # the fact, CONTEXTUALIZES the retry rather than commanding or
        # dismissing it, and names the escalation conditionally — "if it
        # repeats" is honest on the wizard (it will not) and on the phone (it
        # may). Both escalations are already on the verify-fail screen.
        #
        # NOT an owner ruling: #1924's body offers remedies explicitly labelled
        # "not decisions", and the issue carries no ruling comment. This
        # wording is the pipeline's call, derived from #1927's mechanics above,
        # and is the owner's to change.
        RetryableReasonCopy(
            "The microphone's levels changed between measurements, so this "
            "check couldn't settle.",
            "Try again — if it repeats, re-measure, or undo to restore the "
            "previous sound.",
        ),
    ),
    REASON_LOW_ALIGNMENT_CONFIDENCE: _retriable_reason(
        REASON_LOW_ALIGNMENT_CONFIDENCE, TEMPLATE_FIX_AND_RETRY, 1,
        RetryableReasonCopy(
            "Alignment is less certain at this mic position.",
            "Place the microphone about 1 m in front of the speaker at tweeter "
            "height, then measure again.",
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
        # count kept here for readability is the session's own ceiling on
        # wider-spot asks — ``_close_cloud_group`` stops at
        # ``GEOMETRY_RETRY_POSITIONS`` — but it is no longer what admits the
        # retake: since the bounded-retry ruling (#2086) every rung spends one
        # of the POSITION's pooled extras, booked to the speaker rather than the
        # household. Before that, this code's own budget and every other code's
        # ran side by side on the same operator.
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
    # PR-L4 item 1, and since the nanny burn-down the only PR-L4 row here.
    # HARD_STOP with budget 0: the defect is systematic, not transient — a
    # second identical measurement reproduces it — and the copy names the one
    # thing a household can actually act on, the declared driver details the
    # level frame is built from. Copy names the ACTION, not the arithmetic.
    # Item 2's two rows were deleted with item 2's refusal; a round whose
    # forecast says worse now proceeds and says so in the ledger, so there is
    # no sentence to address to anybody.
    REASON_DRIVER_LEVELS_DISAGREE: ReasonSpec(
        REASON_DRIVER_LEVELS_DISAGREE, TEMPLATE_HARD_STOP, 0, "",
        "The two drivers would not have ended up at matching levels, so JTS "
        "left your speaker alone. Re-check the driver details — sensitivity "
        "and any resistor pad — in speaker setup, then measure again.",
    ),
    # PR-L5 delta-probe rollbacks. All three are TEMPLATE_HARD_STOP with no
    # retry budget: the correction has already been undone, so "try again"
    # would re-run the same measurement into the same defect. Each names what
    # was restored FIRST — a household whose speaker just changed twice needs
    # to know where it ended up before it needs a diagnosis — and then the one
    # thing that would actually change the outcome. No hardware nouns, matching
    # the null-classification copy rule.
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
    # #2291's measured regression. Same promise as the three above — and it is
    # true on the same terms: this row renders only when the restore actually
    # ran, and the failed-restore row below is what renders when it did not.
    # The remedy differs from its neighbours because the finding does: nothing
    # misbehaved, so there is no chain to re-check and no level to drop. The
    # honest next step is a different measurement, which usually means moving
    # the microphone or the speaker.
    REASON_CORRECTION_MEASURED_REGRESSION: ReasonSpec(
        REASON_CORRECTION_MEASURED_REGRESSION, TEMPLATE_HARD_STOP, 0, "",
        "JTS measured your speaker before and after the tuning, and it "
        "measured worse afterwards — so the previous sound has been put back. "
        "Nothing is broken; this room and this speaker position did not suit "
        "the tuning. Moving the speaker a little, or measuring from your usual "
        "listening spot, is what changes this.",
    ),
    # #2291's fail-closed boost, and the one row here that reports a
    # NON-finding. Says what JTS could not establish before what it did about
    # it, because the household's speaker changed twice and "why" is otherwise
    # unanswerable from the screen.
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
    # is the row that renders instead — same finding, opposite state of the
    # speaker, and it says so first.
    #
    # ONE row rather than three verdict-specific ones, deliberately. Splitting
    # it would let each keep its own remedy ("move the speaker away from
    # walls"), but that remedy is the SECOND thing this household needs: the
    # first is that a correction they are listening to right now was found
    # faulty and is still applied, and the action is Undo in all three cases.
    # Three near-duplicate rows for a state that should be rare is registry
    # bloat, and the specific finding is on the verdict itself
    # (``delta_probe.verdict``, in the payload and the journal) for whoever
    # needs it after the undo.
    REASON_CORRECTION_ROLLBACK_FAILED: ReasonSpec(
        REASON_CORRECTION_ROLLBACK_FAILED, TEMPLATE_HARD_STOP, 0, "",
        "JTS checked the tuning against what your speaker actually did, and "
        "they did not match — but it could not put the previous sound back on "
        "its own, so the new tuning is STILL APPLIED. Tap Undo on the speaker "
        "page to restore the previous sound.",
    ),
}

# The transient codes whose first retry is automatic (a banner, no decision
# screen) per §5.10 template 1.
TRANSIENT_AUTO_RETRY_CODES = frozenset(
    code for code, spec in REASON_REGISTRY.items()
    if spec.template == TEMPLATE_SILENT_AUTO_RETRY
)

#: #2291 Phase 5a-iv: the capture-consuming ladders' refusal KINDS, mapped to
#: the codes whose copy the household reads.
#:
#: :mod:`jasper.active_speaker.crossover_v2.spatial` owns the ORDER those
#: ladders run in and returns a kind; this file owns the registry above, so the
#: sentence stays here. The same split :mod:`.crossover_v2.coordinator` makes
#: with :data:`~jasper.active_speaker.crossover_v2.coordinator.REFUSAL_KINDS`,
#: and it is a mapping rather than an identity because two kinds do NOT share
#: their code's name: a glitched timeline renders as
#: ``drift_baselines_disagree`` and a bent curve as ``agc_behavioral_fail``.
#:
#: Completeness is CHECKED, not trusted — see :func:`_screen_refusal_code` and
#: ``test_every_screen_kind_has_a_household_sentence``.
SCREEN_KIND_REASONS: dict[str, str] = {
    _spatial.SCREEN_LOCATE_FAILED: REASON_LOCATE_FAILED,
    _spatial.SCREEN_PILOT_LEVEL_COLLAPSE: REASON_PILOT_LEVEL_COLLAPSE,
    _spatial.SCREEN_LINEARITY_FAILED: REASON_AGC_BEHAVIORAL_FAIL,
    _spatial.SCREEN_CAPTURE_GLITCH: REASON_DRIFT_BASELINES_DISAGREE,
    _spatial.SCREEN_CLIPPED: REASON_CLIPPED,
    # The six an ANCHOR phase adds (#2291 Phase 5a-vii, plus #2644's). Two of
    # them do not share their code's name either: an unresolved alignment
    # renders as ``delay_exceeds_search_window``, and a bent curve the room
    # caused renders as ``noisy_room_linearity`` rather than blaming the phone's
    # microphone.
    _dispatch.SCREEN_ANCHOR_AMBIGUOUS: REASON_ANCHOR_AMBIGUOUS,
    _dispatch.SCREEN_CHANNEL_MAP_MISMATCH: REASON_CHANNEL_MAP_MISMATCH,
    _dispatch.SCREEN_SNR_FLOOR: REASON_SNR_FLOOR,
    _dispatch.SCREEN_NOISY_ROOM_LINEARITY: REASON_NOISY_ROOM_LINEARITY,
    _dispatch.SCREEN_ALIGNMENT_UNRESOLVED: REASON_DELAY_EXCEEDS_SEARCH_WINDOW,
    _dispatch.SCREEN_LOW_ALIGNMENT_CONFIDENCE: REASON_LOW_ALIGNMENT_CONFIDENCE,
}


def _screen_refusal_code(kind: str) -> str:
    """One screen kind's household code, LOUDLY on an unmapped one.

    A kind arriving here unmapped is a wiring defect — a new ladder step shipped
    without a sentence — and answering it with another kind's copy is the shape
    :meth:`CrossoverV2Session._round_refusal_for` already refuses. It still
    returns rather than raising, under the most conservative code available: the
    capture was screened and something was wrong with it, and losing that
    refusal to a mapping gap would be worse than naming it imprecisely for one
    release.
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

    One code, two situations, and until #2291 one sentence — which pointed the
    wrong half at a control that cannot help it.

    * **A restore was attempted and did not complete** (``True``/``None``):
      there IS a stored previous sound, the automatic attempt failed, and Undo
      is a real remedy the household can press. Unchanged copy.
    * **There was never an anchor** (``False``): the adoption table routed here
      *because* no previous sound exists, and Undo refuses on that same
      predicate. Telling this household to tap it sends them to a dead end on
      the most ordinary case there is — a speaker's first-ever correction. So
      this arm names no Undo, states what is true about their speaker, and
      offers the two remedies that DO exist.

    ``None`` takes the Undo arm deliberately: an unestablished fact must not
    invent the more alarming claim ("nothing to go back to") about a speaker
    that may well have a perfectly good anchor.
    """
    if rollback_anchor_available is False:
        return (
            "The new tuning is still applied, and this speaker has no stored "
            "previous sound to go back to — this was its first measured "
            "crossover. You can measure again to try for a better result, or "
            "clear the tuning from the Sound page to return to the standard "
            "setup."
        )
    return (
        "JTS checked the tuning against what your speaker actually did, and "
        "they did not match — but it could not put the previous sound back, "
        "so the newer tuning is STILL APPLIED. Tap Undo to restore the "
        "previous sound."
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

    **THE single copy selector**, because one failure is narrated on several
    surfaces that never see each other: the relay verdict the measurement page
    shows the moment a capture is refused
    (:meth:`PhaseVerdict.to_relay_dict`), the envelope jts.local serves for
    the persisted terminal failure
    (``crossover_envelope_v2._reason_message``), the apply-seam refusal, and
    :meth:`_refuse`'s accountability refusals. Two codes now choose their copy
    from evidence rather than holding a literal, and a household looking at
    two of those surfaces after ONE failure must not be handed two different
    accounts of it — which is exactly how the inconclusive copy's own bug
    stayed invisible for as long as it did (#1974). Adding a third
    evidence-keyed code means adding a branch HERE; a caller that renders
    ``spec.message`` directly re-opens the gap.

    Exhaustion is state-aware: :meth:`authorize_begin` keeps the diagnosis
    selected here but replaces retry advice with the terminal outcome. That is
    intentionally not whole-sentence equality. The observation must agree
    across surfaces; an action that is no longer available must not survive.

    ``spec`` is passed in rather than looked up so each caller keeps the
    existence guard it already had — ``REASON_REGISTRY[code]`` raising
    ``KeyError`` on an unregistered code is load-bearing in :meth:`_refuse`,
    whose whole purpose is that a refusal never ships a bare code where a
    household expects a sentence.

    Facts are keyword-only and each defaults to "not established", so a caller
    holding none of them gets the registry's own renderings — the same answer
    reading ``REASON_REGISTRY`` by hand would give.
    """
    if code == REASON_LOCATE_FAILED:
        return locate_failed_message(pilot_heard)
    if code == REASON_VERIFY_INCONCLUSIVE:
        return verify_inconclusive_message(reflection_measured)
    if code == REASON_CORRECTION_ROLLBACK_FAILED:
        return correction_rollback_failed_message(rollback_anchor_available)
    # ``or spec.banner`` for the silent-auto-retry codes, whose household text
    # IS the banner and whose ``message`` is empty by construction.
    return spec.message or spec.banner


def reason_diagnosis(
    code: str,
    spec: ReasonSpec,
    *,
    pilot_heard: bool | None = None,
    reflection_measured: bool | None = None,
) -> str:
    """The observation inside any retryable reason, without retry advice.

    The two evidence-keyed reasons select their diagnosis from this capture's
    facts. Every literal reason reads the diagnosis stored in its structured
    :class:`RetryableReasonCopy`; that same value also composes the registry's
    full retryable ``message``/``banner``. Exhaustion therefore preserves X
    for every retryable code without maintaining a second prose table.
    """
    if code == REASON_LOCATE_FAILED:
        return locate_failed_diagnosis(pilot_heard)
    if code == REASON_VERIFY_INCONCLUSIVE:
        return verify_inconclusive_diagnosis(reflection_measured)
    return spec.retry_copy.diagnosis if spec.retry_copy is not None else ""


# Conditions no extra attempt can clear — wiring in the wrong order, two
# drivers that would not have ended up at matching levels, a dead link. (The
# example that stood in the middle slot, a tuning that would not have improved
# the speaker, left this set with its code: item 2 banks that verdict now and
# the round proceeds.) The bounded-retry
# ruling (#2086) is a CEILING on retries, not a floor: it stops the flow asking
# a household for a fifth take of the same spot, and it does not start it asking
# for a second take of something a second take cannot fix.
#
# A rejection carrying one of these rides out as a TERMINAL capture verdict, at
# the settle — :data:`~.admission.SETTLE_CONDITION_NOT_RETRIABLE`, the ladder's
# first rung — with its own copy and however many extras the slot still has, so
# the phone renders its terminal screen instead of a "Try again" button the next
# begin would refuse. ``assess_begin``'s ``REFUSE_NON_RETRIABLE`` is the
# BACKSTOP for a begin that reaches a settled slot anyway (a page that ignored
# the terminal verdict, a replayed event), not the ordinary path.
NON_RETRIABLE_CODES = frozenset(
    code for code, spec in REASON_REGISTRY.items() if spec.retry_budget == 0
)


@dataclass(frozen=True)
class PhaseVerdict:
    """A consume verdict: the relay dict + the internal reason (if any)."""

    accepted: bool
    code: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    # Whether THIS capture's leading pilot pair cleared the room's own in-band
    # floor — ``analysis.pilot_snr_ok``, carried verbatim including its ``None``
    # (no pilot evidence). The one fact ``locate_failed``'s copy branches on
    # (#2085): it is the direct, same-capture refutation of "couldn't hear the
    # speaker", so it has to reach the sentence. Carried on the verdict rather
    # than dug out of ``payload`` because it is decided at the gate, where the
    # analysis is in hand, and a typed field cannot be misspelled into silence.
    pilot_heard: bool | None = None
    # VERIFY's gate discriminator for ``verify_inconclusive`` (#1974/#2095),
    # paired with the verdict for the same reason ``pilot_heard`` is: terminal
    # exhaustion must repeat this capture's diagnosis, not the registry's
    # evidence-unknown fallback.
    reflection_measured: bool | None = None

    def to_relay_dict(self) -> dict[str, Any]:
        """The mapping ``consume_capture`` returns to ``run_capture_plan``.

        Always carries ``accepted``; a rejection adds the reason code + template
        + copy so the phone renders the right §5.10 screen. Every non-``accepted``
        field is relayed verbatim in the ``capture_result`` host event.

        ``reason`` comes from :func:`reason_message`, not from the registry
        entry directly, so a code whose honest sentence depends on what was
        measured renders that sentence HERE — on the surface the household is
        actually looking at when a capture is refused — and not only in the
        envelope served later. ``pilot_heard`` rides out beside it so the
        journal and the phone's own record can tell the two accounts apart
        without re-deriving the discriminator.
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
