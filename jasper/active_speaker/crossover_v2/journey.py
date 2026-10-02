# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The phases a run's takes are planned and banked under (#2291)."""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# phase vocabulary
# --------------------------------------------------------------------------- #

PHASE_CHECK = "check"
PHASE_MEASURE = "measure"
# An apply in progress: a control-page phase with no capture index.
PHASE_APPLYING = "applying"
PHASE_VERIFY = "verify"
# The two POSITION-GROUP phases (flat-linearization PR-3b). Each spans MANY
# capture-plan indexes — one prompted mic position per index — where every other
# phase spans exactly one. These are SESSION phases, deliberately distinct from
# the excitation program's own ``program.phase``: every cloud position plays the
# VERIFY-shaped mono summed sweep (``phase="verify"``), so
# ``program_analysis.analyze_program_capture`` routes it to ``_analyze_verify``
# with no dispatch change. Do not conflate the two vocabularies.
PHASE_CLOUD_MEASURE = "cloud_measure"
PHASE_CLOUD_VERIFY = "cloud_verify"
# R16 lateral evidence (plan §4.4). A position group like the two clouds, but
# its captures replay the ANCHOR's per-driver MEASURE program rather than the
# summed sweep, so it is NOT in ``SUMMED_SWEEP_PHASES``: same protected-neutral
# commissioning graph, same stimulus, same gains as MEASURE.
PHASE_LATERAL = "lateral"
# The ADR-0319 timing take: the front drivers summed at the design-axis mark,
# MEASURE's in-session prior. One capture at one mark, so not a
# :data:`GROUP_PHASES` member.
PHASE_TIMING = "timing"
# Measured, awaiting an explicit candidate decision; nothing has been applied.
PHASE_REVIEW = "review"
# Every capture in an applied or measurement-free session has finished.
PHASE_DONE = "done"

# The capturing phases in CANONICAL ORDER — the ones bound to the capture
# session's evidence and invalidated on a new session (§5.6). A given session
# runs a SUBSET of these, so a journey walks its own
# :attr:`JourneyPlan.phases` rather than this tuple. Consumers that only have
# the persisted state read its ``session_phases`` field and fall back to this.
CAPTURE_PHASES = (
    PHASE_CHECK,
    PHASE_MEASURE,
    PHASE_LATERAL,
    PHASE_CLOUD_MEASURE,
    PHASE_TIMING,
    PHASE_VERIFY,
    PHASE_CLOUD_VERIFY,
)

# What a session ran before the position groups shipped. Durable state written
# then carries no ``session_phases`` field, so this — not the now-longer
# ``CAPTURE_PHASES`` — is the honest fallback for reading such a state.
PRE_CLOUD_CAPTURE_PHASES = (PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY)

# The phases whose accepted-capture bookkeeping is PER INDEX rather than per
# phase, because one phase spans many prompted positions.
GROUP_PHASES = frozenset({PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY, PHASE_LATERAL})

# --------------------------------------------------------------------------- #
# who READS a lateral group
# --------------------------------------------------------------------------- #
#
# ``PHASE_LATERAL`` says what a group PLAYS; a CONSUMER says who reads it. The
# two walks play identically and differ in WHICH POSE TABLE they run, which is
# the distinction the validator below enforces.

#: The walk over the ratified pose table. The DEFAULT. Its historical spelling,
#: kept because the string is banked on every round that ran one.
LATERAL_CONSUMER_FC_SELECTOR = "fc_selector"

#: An operator-staged walk over poses the request itself states, banked for the
#: offline P2 forward model.
LATERAL_CONSUMER_FORWARD_MODEL = "forward_model_evidence"

LATERAL_CONSUMERS = (LATERAL_CONSUMER_FC_SELECTOR, LATERAL_CONSUMER_FORWARD_MODEL)


def validated_lateral_consumer(consumer: str, *, states_own_poses: bool) -> str:
    """Return ``consumer``, or raise :class:`ValueError`.

    ``consumer`` must be in :data:`LATERAL_CONSUMERS`, and states its own poses
    if and only if it is :data:`LATERAL_CONSUMER_FORWARD_MODEL` — the ratified
    table is the other walk's and an evidence walk may not borrow it.
    """
    if consumer not in LATERAL_CONSUMERS:
        raise ValueError(
            f"a lateral consumer must be one of {LATERAL_CONSUMERS}, "
            f"got {consumer!r}"
        )
    if states_own_poses != (consumer == LATERAL_CONSUMER_FORWARD_MODEL):
        raise ValueError(
            f"exactly the {LATERAL_CONSUMER_FORWARD_MODEL} walk states its own "
            f"poses: {LATERAL_CONSUMER_FC_SELECTOR} runs over the ratified "
            "table, and neither walk may borrow the other's"
        )
    return consumer
