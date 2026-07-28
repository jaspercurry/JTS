# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The v2 crossover conductor — phase orchestration (Wave 5a).

``docs/crossover-measurement-productization-design.md`` §5 replaces the legacy
per-driver distributed transaction with a **conductor**: the Pi compiles one
excitation program per phase, plays it as one continuous stream, and analyzes
``(program, capture) → analysis`` as a pure function. This module owns the
phase state machine that drives the relay session — 16 captures at the FULL
tier's shipped defaults (7 on the express tier, ``TIER_EXPRESS``), since the
spatial cloud replaced the original three:

    CHECK → gain solve → MEASURE → the pre-apply position group → fit +
      candidate → APPLYING (auto) → VERIFY → the post-apply position group
      → done

**Owner decision (2026-07-27): the fit is the last thing before the apply.**
The candidate used to be built the moment MEASURE was accepted, which put it
eight captures BEFORE the pre-apply cloud whose honesty verdict it is supposed
to consume — so the two optional cloud terms in ``compose_envelope`` had no
reachable production caller. Building it at the group close instead lets the
fit correct the envelope around the interference the cloud identified and
refuse to fill it (flat-linearization plan, interpretation call (A)). MEASURE
keeps every trust gate it owned: they read the analysis, not the candidate, so
a session doomed at sweep two still fails at sweep two rather than after a nine
-position walk. A session with no pre-apply group (the pre-cloud 3-entry shape
this class still defaults to) has nothing to wait for and still builds at
MEASURE, with the same accept, the same payload keys and the same apply timing
it had before the move — its ``candidate.json`` does gain an always-empty
``exclusion_evidence`` key, which leaves the fingerprint unchanged.
See :meth:`CrossoverV2Conductor._measure_verdict`.

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
relay session (a heterogeneous ``CapturePlan`` — 16 entries at the full tier's
shipped defaults, 7 on express: check / measure / the pre-apply position group /
verify / the post-apply position group, which express omits entirely; see
"position-group choreography" below)
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
import time
from dataclasses import dataclass, field
from functools import lru_cache
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
from jasper.audio_measurement.excitation_admission import FrequencyBand
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
    GainPlan,
    MeasurementGeometry,
    MeasurementPriors,
    ProgramAnalysis,
    RealizedLevelMatch,
    overlap_band_hz,
    predicted_branch_sum,
    realized_branch_level_match,
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
# The two POSITION-GROUP phases (flat-linearization PR-3b). Each spans MANY
# capture-plan indexes — one prompted mic position per index — where every
# other phase spans exactly one. CLOUD_MEASURE holds the pre-apply spatial
# cloud (the N−1 summed sweeps that follow MEASURE's design-axis anchor);
# CLOUD_VERIFY holds the post-apply one (the M−1 that follow VERIFY's
# anchor). See ``CLOUD_POSITION_PROMPTS`` for the physics the prompts encode
# and ``build_v2_cloud_index_phase_map`` for the index layout.
#
# These are CONDUCTOR phases, deliberately distinct from the EXCITATION
# PROGRAM's own ``program.phase``: every cloud position plays the VERIFY-
# shaped mono summed sweep (``phase="verify"``), so
# ``program_analysis.analyze_program_capture`` routes it to ``_analyze_verify``
# with no dispatch change and the conductor still knows which group the
# capture belongs to. Do not conflate the two vocabularies.
PHASE_CLOUD_MEASURE = "cloud_measure"
PHASE_CLOUD_VERIFY = "cloud_verify"
PHASE_DONE = "done"

# Capture-plan index → phase. APPLYING is a control-page phase (no capture)
# that sits between MEASURE-accepted and VERIFY-armed, so it has no index.
# This is the pre-cloud 3-entry layout, kept as the fallback for a conductor
# constructed with no explicit ``index_phase_map``; the shipped session builds
# its map through ``build_v2_cloud_index_phase_map``.
_INDEX_PHASE = {1: PHASE_CHECK, 2: PHASE_MEASURE, 3: PHASE_VERIFY}
_PHASE_INDEX = {phase: index for index, phase in _INDEX_PHASE.items()}
CAPTURE_PLAN_TARGET = 3

# This flow's own capture retry budget: the total admission attempts a v2
# session may spend across its entries, including retaken captures.
#
# It is deliberately NOT `capture_relay.spec.MAX_CAPTURE_PLAN_ATTEMPTS`. Both
# builders below passed that ceiling verbatim while the two happened to be
# equal, which silently conflated a TRANSPORT limit (how many blob keys the
# relay Worker will store for one session) with a POLICY choice (how many
# retakes this measurement offers a household). Raising the transport ceiling
# to 32 for multi-position capture plans separated them, and this constant
# holds the shipped value so the 3-entry and 1-entry flows keep emitting the
# exact same `max_attempts` on the wire. Changing it is a product decision
# about retries, not a consequence of the relay's capacity.
CAPTURE_PLAN_MAX_ATTEMPTS = 8

# The capturing phases in CANONICAL ORDER — the ones bound to the relay
# session's evidence and invalidated on a new session (§5.6). A given session
# runs a SUBSET of these (a verify-only re-arm runs just ``PHASE_VERIFY``), so
# ``CrossoverV2Conductor`` walks its own ``session_phases`` — the subset its
# ``index_phase_map`` actually addresses, in this order — never this tuple
# directly. Consumers that only have the persisted state read its
# ``session_phases`` field and fall back to this tuple (see
# ``jasper.web.correction_crossover_v2._phase_from_state``).
CAPTURE_PHASES = (
    PHASE_CHECK,
    PHASE_MEASURE,
    PHASE_CLOUD_MEASURE,
    PHASE_VERIFY,
    PHASE_CLOUD_VERIFY,
)

# The phases whose accepted-capture bookkeeping is PER INDEX rather than per
# phase, because one phase spans many prompted positions.
GROUP_PHASES = frozenset({PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY})

# What a session ran before the position groups shipped. Durable state written
# then carries no ``session_phases`` field, and it came from a session that ran
# exactly these three — so this, not the (now longer) ``CAPTURE_PHASES``, is the
# honest fallback for reading such a state. Reading a pre-cloud state against
# the full tuple would report a household mid-"cloud_measure" in a session that
# never had one.
PRE_CLOUD_CAPTURE_PHASES = (PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY)

# The phases whose excitation is the mono summed sweep played through the LIVE
# production graph with no program-graph load and no play-time admission gate
# (see ``jasper.web.correction_crossover_v2.bind_production_play``). VERIFY has
# always been one; the two cloud groups join it because a spatial cloud measures
# the SUMMED system — pre-apply for CLOUD_MEASURE ("what the speaker does
# today"), post-apply for CLOUD_VERIFY. The compose-time min-cap clamp in
# ``_compose_verify_program`` is the only level guard for all three, and its
# argument ("a summed signal reaches every driver, so clamp to the most
# restrictive cap") holds identically before and after apply.
SUMMED_SWEEP_PHASES = frozenset(
    {PHASE_VERIFY, PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY}
)

# --------------------------------------------------------------------------- #
# position-group choreography (flat-linearization PR-3b)
# --------------------------------------------------------------------------- #
#
# docs/flat-linearization-plan.md fundamental 1: "Spatial multi-capture is THE
# measurement... N≈8–12 gated sweeps at guided positions (≥10 cm spread for HF
# null decorrelation; ≥~30 cm spread to support the LF edge)". These constants
# are the product's realisation of that fundamental.

# Total MIC POSITIONS in the pre-apply cloud, MEASURE's design-axis anchor
# included — so the plan emits ``N − 1`` additional prompted positions after
# MEASURE.
#
# Read that literally: the cloud carries ``N − 1`` SUMMED CURVES, not N. The
# anchor is a per-driver MEASURE capture, so ``_analyze_measure`` produces no
# ``summed_response`` for it to contribute and only a modelled
# ``predicted_sum``. The same holds for the post-apply group below, where
# VERIFY's anchor DOES capture a summed sweep but is consumed by the tracking
# verdict rather than joined to the group.
#
# 9 is chosen so that ``N − 1`` = 8 CURVES, which is what
# docs/flat-linearization-plan.md fundamental 1's "N≈8–12 gated sweeps" floor
# actually asks for (adjudication 3a, 2026-07-26: the first draft shipped 8
# positions ⇒ 7 curves, meeting the floor in positions but not in the thing
# that gets combined). Beyond that floor it is a WALL-CLOCK choice, not a
# statistical optimum: S0's stability work (6-of-10 subsets,
# docs/flat-linearization-plan.md "S0 executed") says more positions is
# strictly better, and the session-length ceiling is what stops us at 9. Treat
# it as a constant, never as a promise about accuracy.
DEFAULT_CLOUD_MEASURE_POSITIONS = 9
# The floor a caller may configure. Below 6 the cloud stops decorrelating HF
# nulls well enough to be worth the extra session minutes, and
# ``CLOUD_POSITION_PROMPTS``' wide-offset guarantee (below) is specified
# against exactly this number.
MIN_CLOUD_MEASURE_POSITIONS = 6
# The ceiling a caller may configure. Sized so the worst-case plan still fits
# the relay's blob-index space — see ``assert_cloud_plan_fits_relay_capacity``,
# which is the executable form of that claim.
MAX_CLOUD_MEASURE_POSITIONS = 12
# Total MIC POSITIONS in the post-apply cloud, VERIFY's anchor included — so
# the plan emits ``M − 1`` additional prompted positions after VERIFY, and the
# group combines ``M − 1`` curves (see the positions-are-not-curves note
# above: VERIFY's own summed capture is consumed by the tracking verdict, which
# is a different question than "is the speaker flat"). Smaller than the
# pre-apply cloud on purpose: the post-apply pass grades a correction the
# pre-apply cloud already constrained, and it is paid at the END of a long
# session where operator patience is the binding resource.
DEFAULT_CLOUD_VERIFY_POSITIONS = 6
# The floor a caller may configure for the POST-apply group. It exists for the
# same reason ``MIN_CLOUD_MEASURE_POSITIONS`` does and is enforced the same way:
# both groups walk ``CLOUD_POSITION_PROMPTS`` from the front, so a group that
# stops before the second wide offset carries no ~30 cm-class spread at all and
# silently voids fundamental 1's LF-edge guarantee — which
# ``test_cloud_prompts_front_load_the_wide_offsets`` states as a property of the
# TABLE, not of the default. Until this floor existed, ``M = 2`` was accepted
# and quietly broke that claim.
#
# DERIVED from the table (``_min_positions_for_two_wide_offsets``), never a
# literal: reordering the prompts must move the floor with them, not leave a
# stale number behind.
MIN_CLOUD_VERIFY_POSITIONS = 5

# How many wider-spread RETAKES of the group's last position the
# geometry-locked check may ask for, once per group.
#
# Retakes rather than appended positions for ONE reason, and it is the protocol
# rather than the physics: the relay runner completes a set at exactly
# ``capture_target`` accepted captures with ``index == accepted_count + 1``, so
# rejecting a capture is the only lever that keeps a plan alive at the same
# index — appending would need a variable-length plan the shipped runner cannot
# express.
#
# A "replacing is better physics" argument was made and WITHDRAWN under review
# (2026-07-26): the reviewer computed the power-mean counterexample, where
# APPENDING a wide position to a clustered cloud fills a −15 dB null further
# than replacing does (−6.1 dB vs −7.7 dB) and lowers ``clustered_fraction``
# more besides. Replacing is what the protocol permits, not what the estimator
# prefers; if the runner ever grows variable-length sets, appending is the
# better answer.
#
# Bounded on purpose: `geometry.locked` is a "spread the mic further" hint, not
# a failure, and an unbounded loop against a genuinely position-invariant
# defect (S0's source-fixed horn-rim comb — see the plan doc's "S0 executed"
# §b) would never terminate, because no amount of mic movement decorrelates a
# source-fixed null. Two retakes, then proceed and RECORD the verdict — it
# lands in the journal and the durable v2 state's `cloud` block. PR-4 carries
# it further: `_geometry_guidance_copy`'s plain-language guidance rides the
# envelope's own `cloud` key and `/state`'s compact projection
# (`crossover_v2_status_block`) — but no household-facing surface renders it
# yet (zero JS/asset changes in PR-4). PR-7 renders it.
GEOMETRY_RETRY_POSITIONS = 2

# Retake headroom a cloud plan carries ABOVE its entry count and its geometry
# retries. Deliberately the same ABSOLUTE spare the shipped 3-entry flow has
# always had (``CAPTURE_PLAN_MAX_ATTEMPTS - CAPTURE_PLAN_TARGET`` = 5), not the
# same RATIO: `capture_relay.spec.MAX_CAPTURE_PLAN_ATTEMPTS`' own sizing note
# says longer sets getting proportionally fewer retakes each "is the intended
# direction — a 21-position session that needs 11 retakes has a problem retries
# will not fix."
CLOUD_RETAKE_ALLOWANCE = CAPTURE_PLAN_MAX_ATTEMPTS - CAPTURE_PLAN_TARGET


@dataclass(frozen=True)
class CloudPositionPrompt:
    """One prompted mic move in a position group.

    Split into ``headline`` + ``detail`` by the flow-simplification redesign
    (§2.1): the step screen shows ONE imperative sentence where the counter
    used to be, with at most one short supporting clause under it. Before the
    split this was a single 2-3 sentence paragraph rendered as muted 0.9 rem
    body text under a headline that was just a counter — the inversion the
    owner asked for. ``detail`` may be empty; ``text`` re-joins the two for
    the durable evidence sidecar, which wants the whole instruction the
    operator actually followed rather than only its first sentence.

    ``wide`` marks the moves that carry the plan's ~30 cm-class offset (a
    forearm rather than a hand-width). The flag is not decoration: fundamental
    1 needs ≥10 cm of spread to decorrelate HF nulls and ≥~30 cm to support
    the LF edge, so ``CLOUD_POSITION_PROMPTS`` is ORDERED to put two wide
    moves inside the first ``MIN_CLOUD_MEASURE_POSITIONS - 1`` offsets —
    pinned by test, because an editor reordering this table for readability
    would silently delete the LF half of the measurement.
    """

    headline: str
    detail: str = ""
    wide: bool = False

    @property
    def text(self) -> str:
        """Headline + detail as one string — the evidence sidecar's ``prompt``.

        The sidecar is the only durable statement of WHERE a curve was
        measured, so it records the complete instruction, not the screen's
        headline slot alone.
        """
        return f"{self.headline} {self.detail}".strip() if self.detail else self.headline


# The prompt table, in the order a group walks it.
#
# Copy provenance: the validated reference is the S0 kit's ``_prompt_position``
# table (captures/flat-linearization-20260725/s0-kit/s0_capture.py), whose
# hand-width/forearm language was an owner request from the 2026-07-25 studio
# session after numeric prompts ("move the mic 10 cm left") proved unusable
# standing next to a speaker holding a mic stand. Product copy keeps that
# register — casual, body-relative, never numeric-precision — and stays
# hardware-blind: no horn, no JTS3, nothing that assumes a particular cabinet.
#
# ONE ordered table serves both groups: the pre-apply group uses
# ``[:N - 1]`` and the post-apply group ``[:M - 1]``, so whichever group ends
# soonest still gets the front-loaded spread. That is why the two wide moves
# sit at offsets 3 and 4 rather than at the end, where the S0 kit (which always
# ran all ten) could afford to put them.
CLOUD_POSITION_PROMPTS: tuple[CloudPositionPrompt, ...] = (
    CloudPositionPrompt("Move one hand-width LEFT of the mark."),
    CloudPositionPrompt("Now move one hand-width RIGHT of the mark."),
    CloudPositionPrompt(
        "Move a forearm's length LEFT of the mark.",
        "Step a little toward the speaker as you go, and keep the phone "
        "pointed at it.",
        wide=True,
    ),
    CloudPositionPrompt(
        "Now the same on the RIGHT: a forearm's length out.",
        "A step toward the speaker again, phone still pointed at it.",
        wide=True,
    ),
    CloudPositionPrompt("Come back over the mark, one hand-width HIGHER."),
    CloudPositionPrompt("Now over the mark again, one hand-width LOWER."),
    CloudPositionPrompt("Move two hand-widths LEFT of the mark."),
    CloudPositionPrompt("Now move two hand-widths RIGHT of the mark."),
    CloudPositionPrompt(
        "Come back to the mark's height, then step to a spot you have not "
        "used yet.",
        "A little diagonal off the mark is perfect.",
    ),
    CloudPositionPrompt(
        "Raise the phone a forearm's length ABOVE the mark.",
        "Keep it pointed at the speaker.",
        wide=True,
    ),
    CloudPositionPrompt(
        "Now lower it a forearm's length BELOW the mark.",
        "Keep it pointed at the speaker.",
        wide=True,
    ),
)

# What the household reads during the apply hold, and the same entry's fallback
# screen body. It carries a REPOSITION instruction because the pre-apply cloud
# ends at a wide offset while VERIFY's tracking comparator is only meaningful
# back on the design axis — the hold is the walk-back window.
VERIFY_ANCHOR_HOLD_MESSAGE = (
    "Applying the measured crossover to your speaker. While that finishes, put "
    "the phone back on the mark — same spot, same height, pointed at the "
    "speaker."
)

# The one sentence the 1-entry re-verify re-arm leads with, on BOTH of its
# surfaces (the consent screen's steps and the plan entry's own instruction).
# Flow-simplification §2.4: the 2026-07-27 hardware session abandoned this
# recovery because nothing on screen said it was one sweep rather than another
# walk. Kept as a constant so the two surfaces cannot drift apart.
REVERIFY_NO_REWALK_HEADLINE = (
    "One sweep, back at the mark — you do NOT need to redo the walk."
)

# What the geometry-locked retake asks for. Two rungs, so a second retake is a
# genuinely different instruction rather than the same sentence twice.
CLOUD_GEOMETRY_RETRY_PROMPTS: tuple[str, ...] = (
    "Same measurement, wider spot: take this one about two forearms' length "
    "to the LEFT of the mark, still pointed at the speaker.",
    "One more, wider still: about two forearms' length to the RIGHT of the "
    "mark, and a little higher or lower than before.",
)


def _min_positions_for_two_wide_offsets() -> int:
    """Smallest group size whose walked offsets include two WIDE moves.

    DERIVED from :data:`CLOUD_POSITION_PROMPTS`, never hardcoded: the whole
    point of the wide-offset guarantee is that it survives someone reordering
    that table, and a literal here would be the first thing to go stale if they
    did. A group of size ``g`` walks offsets ``[:g - 1]``, so the answer is one
    past the index of the second wide prompt.
    """
    wide = [i for i, prompt in enumerate(CLOUD_POSITION_PROMPTS) if prompt.wide]
    if len(wide) < 2:
        raise CrossoverV2FlowError(
            "CLOUD_POSITION_PROMPTS must supply at least two wide offsets — "
            "fundamental 1's LF edge needs ~30 cm-class spread"
        )
    return wide[1] + 2


# --------------------------------------------------------------------------- #
# commission tiers (flow-simplification §1)
# --------------------------------------------------------------------------- #

# The two named plan SHAPES a household can consent to. A tier is not a
# loosened floor — it is a distinct, validated (N, M) pair with its own rules,
# so ``MIN_CLOUD_MEASURE_POSITIONS`` (the FULL tier's validated floor) never
# moves to accommodate express.
TIER_FULL = "full"
TIER_EXPRESS = "express"
TIERS = (TIER_FULL, TIER_EXPRESS)
DEFAULT_TIER = TIER_FULL

# Express's post-apply group: VERIFY's design-axis anchor and nothing else. An
# ``M = 1`` plan emits NO cloud-verify entries, so express makes no
# cross-position post-apply claim at all — it verifies tracking at the mark
# (``VERIFY_TOLERANCE_DB``, unchanged) and says so. See the degraded-claims
# table in docs/flat-linearization-flow-simplification-plan.md §1.3.
EXPRESS_CLOUD_VERIFY_POSITIONS = 1


def express_cloud_measure_positions() -> int:
    """Express's pre-apply group size — DERIVED, never the literal 5.

    Express walks the shortest prompted cloud that still contains BOTH of
    :data:`CLOUD_POSITION_PROMPTS`' wide (~30 cm-class) moves, which is
    exactly what :func:`_min_positions_for_two_wide_offsets` computes and
    exactly why :data:`MIN_CLOUD_VERIFY_POSITIONS` is derived the same way: if
    the table's wide moves are ever reordered, express must move with them
    rather than silently ship one-wide and void fundamental 1's LF-edge
    guarantee.
    """
    return _min_positions_for_two_wide_offsets()


@dataclass(frozen=True)
class V2PlanShape:
    """The RESOLVED (tier, N, M) triple — one value, threaded everywhere.

    Before this existed, ``prepare_v2_session`` called
    :func:`build_v2_session_spec` and :func:`build_v2_cloud_index_phase_map`
    with independent defaults and passed counts to neither: two functions that
    MUST agree, agreeing only by luck. Resolving once and threading the result
    closes that desync hazard by construction — the plan the phone is handed
    and the index→phase map the conductor walks are derived from the same
    object or they are not built at all.
    """

    tier: str
    cloud_measure_positions: int
    cloud_verify_positions: int

    @property
    def capture_target(self) -> int:
        """Accepted captures this shape runs (``1 + N + M``)."""
        return 1 + self.cloud_measure_positions + self.cloud_verify_positions

    @property
    def max_attempts(self) -> int:
        """This shape's admission budget (entries + geometry retakes + spare)."""
        return self.capture_target + GEOMETRY_RETRY_POSITIONS + CLOUD_RETAKE_ALLOWANCE

    @property
    def has_cloud_verify_group(self) -> bool:
        """Whether this shape emits a post-apply position GROUP at all.

        ``False`` for express (``M = 1``): VERIFY's anchor is the last entry,
        so the plan's end-screen copy rides IT rather than a group tail.
        """
        return self.cloud_verify_positions > 1


def normalize_tier(tier: Any) -> str:
    """Allowlist a household-supplied tier id; empty/absent means FULL.

    Deliberately strict about the value and lenient about absence: an unset
    tier is every pre-tier caller (and the wizard before PR-U3 ships its
    chooser), which must keep getting the full instrument; an UNKNOWN tier is
    a caller asking for an instrument this build does not have, which must
    fail loudly rather than silently measure something else.
    """
    name = str(tier or "").strip().lower()
    if not name:
        return DEFAULT_TIER
    if name not in TIERS:
        raise CrossoverV2FlowError(
            f"unknown commission tier {name!r} (expected one of {', '.join(TIERS)})"
        )
    return name


def resolve_plan_shape(
    tier: Any = None,
    *,
    cloud_measure_positions: int | None = None,
    cloud_verify_positions: int | None = None,
) -> V2PlanShape:
    """Resolve (and validate) one plan shape from a tier and optional counts.

    Express admits EXACTLY (:func:`express_cloud_measure_positions`,
    :data:`EXPRESS_CLOUD_VERIFY_POSITIONS`) — it is a named shape, not a
    configurable range, so an explicit count that disagrees is a caller bug
    rather than a preference. Full keeps the shipped ranges
    (``MIN_CLOUD_MEASURE_POSITIONS..MAX_CLOUD_MEASURE_POSITIONS``,
    ``M >= MIN_CLOUD_VERIFY_POSITIONS``).
    """
    name = normalize_tier(tier)
    if name == TIER_EXPRESS:
        n = express_cloud_measure_positions()
        m = EXPRESS_CLOUD_VERIFY_POSITIONS
        for label, wanted, got in (
            ("cloud_measure_positions", n, cloud_measure_positions),
            ("cloud_verify_positions", m, cloud_verify_positions),
        ):
            if got is not None and int(got) != wanted:
                raise CrossoverV2FlowError(
                    f"the express tier is a fixed shape: {label} must be "
                    f"{wanted}, got {int(got)}"
                )
        # Still routed through the shared table-length check below, so a
        # shortened prompt table fails here rather than at entry-build time.
        _validated_cloud_counts(
            cloud_measure_positions=n, cloud_verify_positions=m, tier=name,
        )
        return V2PlanShape(
            tier=name, cloud_measure_positions=n, cloud_verify_positions=m,
        )
    n, m = _validated_cloud_counts(
        cloud_measure_positions=(
            DEFAULT_CLOUD_MEASURE_POSITIONS
            if cloud_measure_positions is None
            else cloud_measure_positions
        ),
        cloud_verify_positions=(
            DEFAULT_CLOUD_VERIFY_POSITIONS
            if cloud_verify_positions is None
            else cloud_verify_positions
        ),
        tier=name,
    )
    return V2PlanShape(tier=name, cloud_measure_positions=n, cloud_verify_positions=m)


def _shape_from_kwargs(
    plan_shape: V2PlanShape | None,
    *,
    tier: Any = None,
    cloud_measure_positions: int | None = None,
    cloud_verify_positions: int | None = None,
) -> V2PlanShape:
    """One resolved shape from either a pre-resolved value or loose kwargs.

    Passing both is refused rather than silently preferring one: the whole
    point of :class:`V2PlanShape` is that two surfaces cannot disagree about
    the shape, and a caller handing over two sources of truth has already lost
    that guarantee.
    """
    loose = (tier, cloud_measure_positions, cloud_verify_positions)
    if plan_shape is not None:
        if any(value is not None for value in loose):
            raise CrossoverV2FlowError(
                "pass either plan_shape or explicit tier/position counts, "
                "never both"
            )
        return plan_shape
    return resolve_plan_shape(
        tier,
        cloud_measure_positions=cloud_measure_positions,
        cloud_verify_positions=cloud_verify_positions,
    )


def assert_cloud_plan_fits_relay_capacity() -> None:
    """Raise unless the WORST-CASE cloud plan fits the relay's index space.

    The relay stores one blob per admitted attempt at ``capture_index =
    attempt - 1``, so ``capture_relay.spec.MAX_CAPTURE_PLAN_ATTEMPTS`` bounds
    entries PLUS retakes for a whole session. That ceiling was sized (PR-3a)
    from the choreography constants above; this function is the executable
    statement of the dependency, so raising ``MAX_CLOUD_MEASURE_POSITIONS`` or
    ``DEFAULT_CLOUD_VERIFY_POSITIONS`` past what the relay can carry fails
    here — loudly, in a hardware-free test — instead of stranding an operator
    mid-cloud when a blob index is refused.
    """
    from jasper.capture_relay.spec import MAX_CAPTURE_PLAN_ATTEMPTS

    entries = cloud_capture_target(
        cloud_measure_positions=MAX_CLOUD_MEASURE_POSITIONS,
        cloud_verify_positions=DEFAULT_CLOUD_VERIFY_POSITIONS,
    )
    if entries + GEOMETRY_RETRY_POSITIONS > MAX_CAPTURE_PLAN_ATTEMPTS:
        raise CrossoverV2FlowError(
            f"worst-case cloud plan needs {entries + GEOMETRY_RETRY_POSITIONS} "
            f"relay blob indexes but the relay ceiling is "
            f"{MAX_CAPTURE_PLAN_ATTEMPTS}"
        )
    attempts = cloud_plan_max_attempts(
        cloud_measure_positions=MAX_CLOUD_MEASURE_POSITIONS,
        cloud_verify_positions=DEFAULT_CLOUD_VERIFY_POSITIONS,
    )
    if attempts > MAX_CAPTURE_PLAN_ATTEMPTS:
        raise CrossoverV2FlowError(
            f"worst-case cloud plan's attempt budget {attempts} exceeds the "
            f"relay ceiling {MAX_CAPTURE_PLAN_ATTEMPTS}"
        )


def _validated_cloud_counts(
    *,
    cloud_measure_positions: int,
    cloud_verify_positions: int,
    tier: str = DEFAULT_TIER,
) -> tuple[int, int]:
    """Validate one (N, M) pair AGAINST ITS TIER's rules.

    The FULL tier keeps the shipped ranges verbatim. Express is checked
    against its own derived shape instead — the range rules would reject it
    (that is the point: express is a distinct named plan, not a loosened
    floor), and :func:`resolve_plan_shape` has already pinned N and M to the
    derived constants before calling here. What both tiers share is the
    prompt-table length check below, which is a property of the TABLE.
    """
    n = int(cloud_measure_positions)
    m = int(cloud_verify_positions)
    if tier != TIER_EXPRESS:
        if not MIN_CLOUD_MEASURE_POSITIONS <= n <= MAX_CLOUD_MEASURE_POSITIONS:
            raise CrossoverV2FlowError(
                f"cloud_measure_positions must be "
                f"{MIN_CLOUD_MEASURE_POSITIONS}..{MAX_CLOUD_MEASURE_POSITIONS}, got {n}"
            )
        if m < MIN_CLOUD_VERIFY_POSITIONS:
            raise CrossoverV2FlowError(
                f"cloud_verify_positions must be at least "
                f"{MIN_CLOUD_VERIFY_POSITIONS}, got {m}"
            )
    # Both groups index the SAME prompt table, so the longer of the two bounds
    # how many offsets it must supply.
    offsets_needed = max(n, m) - 1
    if offsets_needed > len(CLOUD_POSITION_PROMPTS):
        raise CrossoverV2FlowError(
            f"cloud group needs {offsets_needed} position prompts but "
            f"CLOUD_POSITION_PROMPTS supplies {len(CLOUD_POSITION_PROMPTS)}"
        )
    return n, m


def cloud_capture_target(
    *,
    plan_shape: V2PlanShape | None = None,
    tier: Any = None,
    cloud_measure_positions: int | None = None,
    cloud_verify_positions: int | None = None,
) -> int:
    """Accepted captures one cloud session runs: CHECK + the two groups.

    ``1 + N + M`` — CHECK, then the pre-apply cloud (MEASURE's anchor plus
    ``N − 1`` prompted positions), then the post-apply cloud (VERIFY's anchor
    plus ``M − 1``). 16 at the full tier's shipped defaults, 7 for express.
    """
    return _shape_from_kwargs(
        plan_shape,
        tier=tier,
        cloud_measure_positions=cloud_measure_positions,
        cloud_verify_positions=cloud_verify_positions,
    ).capture_target


def cloud_plan_max_attempts(
    *,
    plan_shape: V2PlanShape | None = None,
    tier: Any = None,
    cloud_measure_positions: int | None = None,
    cloud_verify_positions: int | None = None,
) -> int:
    """This flow's retry budget for a cloud plan (a POLICY number).

    Entries + the bounded geometry retakes + ``CLOUD_RETAKE_ALLOWANCE``. Kept
    separate from ``capture_relay.spec.MAX_CAPTURE_PLAN_ATTEMPTS`` (the relay's
    TRANSPORT ceiling) for the reason ``CAPTURE_PLAN_MAX_ATTEMPTS`` states:
    conflating the two is how a transport change silently becomes a product
    change. 23 at the full tier's shipped defaults, 14 for express.
    """
    return _shape_from_kwargs(
        plan_shape,
        tier=tier,
        cloud_measure_positions=cloud_measure_positions,
        cloud_verify_positions=cloud_verify_positions,
    ).max_attempts


def build_v2_cloud_index_phase_map(
    *,
    plan_shape: V2PlanShape | None = None,
    tier: Any = None,
    cloud_measure_positions: int | None = None,
    cloud_verify_positions: int | None = None,
) -> dict[int, str]:
    """Capture-plan index → conductor phase for one cloud session.

    The relay drives 1-based indexes where ``index == accepted_count + 1``
    (``capture_relay.session._poll_capture_plan``), so this map is also the
    running order::

        1                    CHECK
        2                    MEASURE            (design-axis anchor)
        3 .. N+1             CLOUD_MEASURE      (N-1 prompted positions)
        N+2                  VERIFY             (design-axis anchor, on_apply)
        N+3 .. N+M+1         CLOUD_VERIFY       (M-1 prompted positions)

    At ``M = 1`` (the express tier) the last line is empty: VERIFY's anchor is
    the final index and the session runs no post-apply group at all.

    Single source of truth: ``build_v2_capture_plan`` builds its entries from
    this same function, so an entry's prompt can never address a different
    phase than the conductor believes it is running.
    """
    shape = _shape_from_kwargs(
        plan_shape,
        tier=tier,
        cloud_measure_positions=cloud_measure_positions,
        cloud_verify_positions=cloud_verify_positions,
    )
    n, m = shape.cloud_measure_positions, shape.cloud_verify_positions
    mapping = {1: PHASE_CHECK, 2: PHASE_MEASURE}
    for offset in range(n - 1):
        mapping[3 + offset] = PHASE_CLOUD_MEASURE
    mapping[n + 2] = PHASE_VERIFY
    for offset in range(m - 1):
        mapping[n + 3 + offset] = PHASE_CLOUD_VERIFY
    return mapping


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
# candidate at the confirm seam, BEFORE the auto-apply thread starts, so the
# speaker is never touched: the honest outcome of "we cannot show this makes
# your speaker better" is to leave it alone and say so.
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
# item 2 — the PREDICTED post-apply response fails the flat spec and is not
# materially better than the measured pre-apply response. Applying it would
# spend the household's speaker on a change we can already show does not help.
REASON_CORRECTION_NOT_AN_IMPROVEMENT = "correction_not_an_improvement"


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
    REASON_CLOUD_GEOMETRY_LOCKED: ReasonSpec(
        REASON_CLOUD_GEOMETRY_LOCKED, TEMPLATE_FIX_AND_RETRY,
        # Budget = the retake count itself. ``authorize_begin`` admits
        # ``retry_budget + 1`` attempts at a slot before refusing, and this
        # code is only ever raised at the group's LAST position, so a budget of
        # GEOMETRY_RETRY_POSITIONS is exactly "the original take plus two
        # wider ones". The conductor stops asking before this budget bites
        # (``_geometry_retries_used``); the budget is the backstop, not the
        # policy.
        GEOMETRY_RETRY_POSITIONS, "",
        # Copy names the ACTION, not the diagnosis — a household has no way to
        # judge "the echo estimates clustered". The per-attempt wider-spot
        # instruction rides the verdict payload's ``prompt`` field on top of
        # this (see ``_cloud_measure_group_verdict``).
        "These spots were too close together to tell a real dip from an echo. "
        "Take this one from further out and we will use it instead.",
    ),
    # PR-L4. Both are HARD_STOP with budget 0: the defects are systematic, not
    # transient — a second identical measurement reproduces them — and both name
    # the one thing a household can actually act on, the declared driver details
    # the level frame is built from. Copy names the ACTION, not the arithmetic.
    REASON_DRIVER_LEVELS_DISAGREE: ReasonSpec(
        REASON_DRIVER_LEVELS_DISAGREE, TEMPLATE_HARD_STOP, 0, "",
        "The two drivers would not have ended up at matching levels, so JTS "
        "left your speaker alone. Re-check the driver details — sensitivity "
        "and any resistor pad — in speaker setup, then measure again.",
    ),
    REASON_CORRECTION_NOT_AN_IMPROVEMENT: ReasonSpec(
        REASON_CORRECTION_NOT_AN_IMPROVEMENT, TEMPLATE_HARD_STOP, 0, "",
        "The tuning JTS worked out would not have made this speaker measure "
        "better, so it was not applied. Re-check the driver details in speaker "
        "setup, then measure again.",
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


# (``_flatness_evidence_from_tracking`` lived here until the
# flat-linearization plan's PR-5. It repackaged one VERIFY capture's own
# grid-and-band-mean flatness number for the RESULT/verify_fail screens; that
# number is retired along with ``program_analysis._flatness_tracking``, and the
# flatness the household sees now comes from the cloud group's spec evaluation
# — ``assemble_cloud_group_result``'s ``flatness`` key, one construction, one
# owner. See that function and ``flat_spec.spec_flatness_gauge``.)


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

# How far the ripple-optimal tweeter trim may move from its ANCHORED trim
# (raw trim + that branch's measured `correction_giveback_db`, normalized) before
# the scan's result is treated as implausible and discarded in favor of the
# anchored pair (with a WARNING — never a silent swap). ANCHOR-anchored since
# the 2026-07-24 JTS3 runs (#1668): the anchor is measured give-back, not a
# solver prediction, so it is trusted by construction and only the SCAN can
# drift. What the guard catches is the scan wandering into the "attenuate the
# tweeter toward silence is always flatter against a flat woofer" degenerate its
# own +/-window allows. It no longer fights the give-back itself: the previous
# overlap-band seed under-returned the correction and this guard then blocked the
# scan's attempt to fix it on both live runs — the anchor removes that conflict.
# Magnitude protection against a garbage correction lives upstream in the fit
# engine's structural caps (per-filter <=12 dB cut, total normalization budget,
# realization tolerance) plus the downstream VERIFY gate — not this trim guard.
# NOTE the margin's MEANING changed with the anchor: it is now "how far the
# summed-flatness optimum may disagree with the measured level anchor," not
# "how far a re-solve may drift from another level estimate." The 6.0 value is
# retained deliberately and is judged from live guard telemetry, not re-derived.
LINEARIZATION_TRIM_SANITY_MARGIN_DB = 6.0

# How much the correction must improve ITS OWN two-branch model before a
# spec-failing prediction is allowed onto the speaker (linearization-integrity
# PR-L4 item 2). Both numbers are the pooled spec residual
# (`flat_spec.spec_convergence_residual`) of the RAW pre-fit and the LINEARIZED
# predicted sum, graded through the identical evaluator, in dB.
#
# The gate only bites when the prediction ALREADY fails the spec — a prediction
# that meets it needs no improvement argument, and gating an in-spec result on
# "how much did it improve" would refuse the flattest speakers hardest. So the
# question this threshold answers is narrow: *we can already see this will not
# reach spec — is it at least clearly moving the right way?*
#
# 0.5 dB, and the derivation changed with the frame (PR-L4 review B1). While
# this compared the model against the measured in-room cloud, the threshold had
# to absorb the whole cross-frame gap and was set at `SPEC_BANDS[0]`'s 1.5 dB
# for that reason — which, as the review demonstrated, made the verdict a
# function of the ROOM rather than the correction. Now that both terms are the
# same instrument (same branches, same grid, same evaluator, differing ONLY by
# the emitted filters) the comparison carries no measurement noise at all, so
# the threshold is a product-policy floor instead of a noise margin.
#
# 0.5 dB is that floor because it is this model's own measured tracking error:
# `_fit_linearization` records the complex-correction model tracking the real
# VERIFY summation to ~0.5 dB on JTS3 (the zero-phase model it replaced
# mistracked by ~2.0 dB). An improvement smaller than the gap between what we
# model and what the hardware realizes is not an improvement we can honestly
# claim, so it does not earn an apply.
PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB = 0.5

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
# Reads whether the conductor's own auto-apply (triggered by the host off the
# candidate-carrying verdict — the CLOUD_MEASURE group close on a cloud
# session, MEASURE's own accept on the pre-cloud shape; §owner rulings
# 2026-07-20 and 2026-07-27) hit a TERMINAL failure —
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
    # Position-group evidence retention (PR-3b), called once per ACCEPTED cloud
    # capture with ``(position_id, capture_result, metadata)``. Optional so
    # every pre-cloud construction site (and every conductor unit test) stays
    # valid; ``None`` means the group runs with no durable per-position
    # artifact, which is the correct behaviour for a conductor with no evidence
    # store rather than a reason to fail a capture.
    retain_position: Callable[[str, Any, Mapping[str, Any]], None] | None = None
    # PR-4: the cloud honesty-pipeline bundle publisher, called once per
    # CLOSED group with ``(phase, cloud_group_result_dict)``. Optional for the
    # same reason ``retain_position`` is: every pre-PR-4 construction site
    # (and every conductor unit test) stays valid, and ``None`` means the
    # group's result is computed and readable via
    # :meth:`CrossoverV2Conductor.group_cloud_result` but not published as a
    # bundle artifact.
    publish_cloud: Callable[[str, Mapping[str, Any]], None] | None = None


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
    # The ordered phases THIS session actually runs — the subset of
    # ``CAPTURE_PHASES`` its ``index_phase_map`` addresses. Persisted so a
    # host reading only the durable state can tell "verify is the last phase
    # of a re-arm session" from "verify is followed by a post-apply cloud",
    # which the module-global tuple cannot express. Empty on state written
    # before PR-3b; readers fall back to ``CAPTURE_PHASES`` then.
    session_phases: tuple[str, ...] = ()
    # WHICH INSTRUMENT produced this session (:data:`TIER_FULL` /
    # :data:`TIER_EXPRESS`). Empty string means UNKNOWN — state written before
    # tiers existed, or a conductor constructed without one — and readers must
    # render it as unknown rather than assuming full, the same
    # unknown-vs-default discipline ``echo_band_provenance`` carries (issue
    # #1763): the two tiers make materially different claims (§1.3), so
    # guessing one would attach a post-apply cross-position claim to a result
    # that never measured across positions.
    tier: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "accepted_phases": list(self.accepted_phases),
            "applied": self.applied,
            "gain_plan_db": dict(self.gain_plan_db) if self.gain_plan_db else None,
            "candidate_fingerprint": self.candidate_fingerprint,
            "session_phases": list(self.session_phases),
            "tier": self.tier,
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


@dataclass(frozen=True)
class _CloudPosition:
    """One accepted position inside a group, retained for the group-end combine.

    ``response`` is the capture's ``ProgramAnalysis.summed_response`` — a
    ``program_analysis.DriverResponse`` carrying the calibrated, reflection-gated
    magnitude on a linear (rfftfreq) grid plus the matching complex TF. Holding
    the response rather than a pre-built
    :class:`~jasper.audio_measurement.spatial_combine.PositionCapture` is
    deliberate: PR-4 needs the same object for the per-position work the null
    gate and the spec curve do, and re-deriving it from a lossy intermediate
    would be the drift this seam exists to prevent.
    """

    position_id: str
    index: int
    attempt: int
    prompt: str
    wide: bool
    captured_at: float
    response: Any
    sample_rate_hz: int
    # PR-4: the contract-derived analysis bands this position's GROUP should be
    # combined/searched with — spatial_combine.combine_positions's own
    # ``echo_band_hz`` / ``signal_band_hz`` kwargs, echoed here rather than
    # threaded as a separate call-site argument. Carrying them on the position
    # (every position in one group shares the same conductor-derived values —
    # see ``CrossoverV2Conductor.__init__``) is what lets
    # :func:`combine_cloud_positions` derive the right bands from
    # ``positions`` alone, with no caller (``_close_cloud_group``'s single
    # combine, ``cloud_geometry_verdict``'s convenience wrapper) needing to
    # pass them explicitly or risk two call sites drifting apart.
    # ``None`` means "use the module defaults" — the pre-PR-4 behaviour, still
    # exercised by every corpus/unit test that builds a ``_CloudPosition``
    # without these two kwargs.
    echo_band_hz: tuple[float, float] | None = None
    signal_band_hz: tuple[float, float] | None = None


def cloud_position_capture(position: _CloudPosition) -> Any:
    """One retained position → a :class:`spatial_combine.PositionCapture`.

    **The PR-4 seam.** PR-3b calls the combiner for one thing — the geometry
    verdict — but the input assembly is the whole assembly, so PR-4's wider
    pipeline (``identify_interference_nulls`` → ``evaluate_flat_spec``) extends
    the consumer, never this builder.

    Regime of the ``ir`` field, stated exactly because ``detect_echo``'s answer
    depends on it: it is the inverse rFFT of the response's **gated, calibrated**
    complex transfer function — i.e. the impulse response AFTER
    ``deconv.direct_arrival_window`` and the adaptive reflection gate that
    ``program_analysis._driver_response`` applies, not the raw deconvolved IR.
    The direct arrival is therefore present (the window places it at a fixed
    pre-offset) and early secondary arrivals inside the gate survive, which is
    the region ``detect_echo`` windows itself down to; LATE room reflections
    beyond the gate are gone by construction. The S0 forensics ran the detector
    on the ungated IR instead — ``tests/test_crossover_v2_cloud_geometry_corpus.py``
    is the measurement that the two agree on the S0 corpus's geometry verdict,
    rather than an assumption that they must.
    """
    from jasper.audio_measurement.spatial_combine import PositionCapture

    response = position.response
    freqs = np.asarray(response.freqs_hz, dtype=float)
    magnitude = np.asarray(response.magnitude_db, dtype=float)
    complex_tf = np.asarray(response.complex_tf)
    # ``program_analysis._n_fft_for`` always returns a power of two (>= 8192),
    # so the analysis grid is an even-length rfft and ``n = 2*(bins-1)``
    # inverts it exactly rather than approximately.
    ir = np.fft.irfft(complex_tf, n=2 * (complex_tf.size - 1))
    return PositionCapture(
        position_id=position.position_id,
        freqs_hz=freqs,
        magnitude_db=magnitude,
        sample_rate=int(position.sample_rate_hz),
        ir=ir,
    )


def combine_cloud_positions(positions: Sequence[_CloudPosition]) -> Any:
    """Assemble a closed group and combine it — the whole PR-4 seam.

    Returns a :class:`~jasper.audio_measurement.spatial_combine.CombinedResponse`,
    or ``None`` when the group cannot be combined (no positions, or a malformed
    one). Called exactly ONCE per group-close event, from
    :meth:`CrossoverV2Conductor._close_cloud_group`: PR-3b reads one field off
    the result (``geometry``, via :func:`_geometry_verdict_from_combined`);
    PR-4's pipeline (:func:`assemble_cloud_group_result`) reads the rest of
    the SAME object. Never a second combine — see S3 review finding
    (2026-07-26): an earlier revision of this wiring called this function
    TWICE per close attempt (once through :func:`cloud_geometry_verdict` for
    the retry gate, once more from the pipeline) — measured seconds-per-combine
    (3-6 s across runs/hosts on the S0 ten-position corpus; interpreter-bound
    ``smooth_fractional_octave``, worse on a Pi 5 — N2 review finding,
    2026-07-27: an earlier "5.6-6.2 s" point figure did not reproduce across
    hosts, so this states the regime instead of a false-precision number).
    ``GEOMETRY_RETRY_POSITIONS = 2`` allows up to 3 close attempts per group
    (2 retries + the accepting close), so the pre-fix worst case was 3 × 2 =
    6 combines, not the earlier "4x" claim — real operator seconds for a
    claim (byte-for-byte determinism) that was true but not worth paying for.

    Never raises. A group's captures are already-accepted evidence and a
    combiner failure must not retroactively fail them, so an unusable cloud is
    a ``None`` the caller turns into an honest "unknown" rather than an
    exception that would strand the session.
    """
    from jasper.audio_measurement.spatial_combine import (
        DEFAULT_ECHO_BAND_HZ,
        combine_positions,
    )

    if not positions:
        return None
    # Every position in one group carries the SAME conductor-derived bands
    # (set once at construction — see ``_CloudPosition``'s docstring), so
    # reading them off the first position is reading the group's own bands,
    # not an arbitrary one. ``None`` (a position built before PR-4, or by a
    # caller that never declared a driver contract) falls back to the
    # module's own long-standing default, unchanged from pre-PR-4 behaviour.
    echo_band_hz = positions[0].echo_band_hz or DEFAULT_ECHO_BAND_HZ
    signal_band_hz = positions[0].signal_band_hz
    try:
        return combine_positions(
            [cloud_position_capture(p) for p in positions],
            echo_band_hz=echo_band_hz,
            signal_band_hz=signal_band_hz,
        )
    except (ValueError, TypeError, IndexError, AttributeError) as exc:
        log_event(
            logger, "correction.crossover_v2_cloud_combine_failed",
            level=logging.WARNING,
            positions=len(positions), error=str(exc),
        )
        return None


def _geometry_verdict_from_combined(
    combined: Any, n_positions: int,
) -> dict[str, Any]:
    """The geometry-verdict dict from an ALREADY-COMBINED result.

    Split out of :func:`cloud_geometry_verdict` (S3 review finding,
    2026-07-26) so :meth:`CrossoverV2Conductor._close_cloud_group` can
    combine a group's positions exactly ONCE and derive both the retry-gating
    verdict and the honest-instrument pipeline from that ONE object, rather
    than each deriving its own combine. A plain JSON-native dict, because the
    host persists it verbatim into the durable v2 state. ``locked`` is
    ``False`` on every degraded path — but the ``reason`` says WHICH degraded
    path, so "no credible echo estimates" never reads the same as "the cloud
    combined and its nulls move".
    """
    if combined is None:
        return {
            "locked": False,
            "reason": "combine_failed",
            "n_positions": n_positions,
        }
    geometry = combined.geometry
    return {
        "locked": bool(geometry.locked),
        "reason": str(geometry.reason),
        "n_confident": int(geometry.n_confident),
        "n_positions": int(geometry.n_positions),
        "median_tau_us": float(geometry.median_tau_us),
        "clustered_fraction": float(geometry.clustered_fraction),
        "thin_evidence": bool(geometry.thin_evidence),
    }


def cloud_geometry_verdict(positions: Sequence[_CloudPosition]) -> dict[str, Any]:
    """PR-3b's one use of the combiner: combine, then read ``.geometry``.

    A convenience wrapper around :func:`combine_cloud_positions` +
    :func:`_geometry_verdict_from_combined` for callers that only have
    ``positions`` (the corpus acceptance test; any future direct caller) —
    the conductor itself does NOT call this (see
    :meth:`CrossoverV2Conductor._close_cloud_group`'s own single combine).

    **Reason-string divergence, documented not silently left (N4 review
    finding, 2026-07-27).** An empty ``positions`` short-circuits HERE with
    ``reason="no_positions"`` before ever reaching the combiner, while
    :func:`_geometry_verdict_from_combined` called directly with a
    ``combined=None`` and ``n_positions=0`` (e.g. because
    ``combine_cloud_positions([])`` was called some other way) reports
    ``reason="combine_failed"`` for the exact same "there were zero
    positions" fact. Unreachable through the conductor today (a group only
    closes with at least its just-captured position already retained), but
    the two functions disagree on naming WHICH degraded path a caller hit —
    the entire point of a ``reason`` field — so this wrapper owns disclosing
    the split rather than leaving a future reader to discover it by diffing
    the two bodies.
    """
    if not positions:
        return {"locked": False, "reason": "no_positions", "n_positions": 0}
    combined = combine_cloud_positions(positions)
    return _geometry_verdict_from_combined(combined, len(positions))


# --------------------------------------------------------------------------- #
# PR-4: contract-derived analysis bands + the live-flow honesty pipeline
# --------------------------------------------------------------------------- #
#
# docs/flat-linearization-productization-plan.md, PR-4: "The echo/detector
# band and PR-2's signal_band_hz derive from the declared contract: the
# summed system's swept band (RoleBand.band as composed) for the passband;
# the tweeter's usable_frequency_range_hz / measurement_band_hz for the upper
# echo band -- replacing DEFAULT_ECHO_BAND_HZ's flat constant at the call
# site." This section is that derivation, plus the single result-assembly
# function issue #1742 item 4 asks for.

# The contract-derived echo/null analysis band's LOWER edge must not drift
# below this floor without disclosure. Provenance, not a new calibration:
# spatial_combine.py's BAND_BELOW_PASSBAND_MARGIN_DB comment (PR-2, N-3) pins
# a six-band sweep of the SAME JTS3 cdhorn corpus this program's corpus tests
# already use --
#
#   band            residue deficit    screen catches it?
#   (5000, 19000)   40.43-41.98 dB     yes  (the module default)
#   (4000, 20000)   35.46-35.58 dB     yes, by 10.46 dB -- comfortable
#   (3000, 19000)   26.53-27.05 dB     yes, by only 1.53 dB -- "already thin
#                                      one octave up"
#   (2000, 19000)   18.21-18.23 dB     NO -- a false negative, not a
#                                      narrowed gap (this speaker's crossover
#                                      sits at 2 kHz; the woofer's own
#                                      passband is inside the analysed band)
#
# re-derived by test_band_deficit_separation_depends_on_the_analysis_band.
# 4000 Hz is the lowest edge in that pinned table with COMFORTABLE headroom
# (10.46 dB, vs the 3 kHz row's thin 1.53 dB) -- the row printed above is
# the one that actually justifies this constant's value.
#
# **A declared contract whose derived echo band dips below this floor is
# CLAMPED up to it, and the clamp is disclosed** (event + payload). PR-4
# shipped the reviewed disclose-don't-override design -- warn, then run the
# detector on the declared band anyway -- and the first real cloud session
# falsified it (2026-07-27, session cap_4NUGqx3yIzSuv4ta2ozfKw; issue
# #1763): the JTS3 tweeter's CORRECTLY declared measurement_band_hz
# [2000, 18000] produced a (2000, 18000) analysis band, fired the designed
# WARNING, and proceeded -- so that session's tau/r/registry outputs carry
# an uncalibrated-regime asterisk on the one measurement that mattered (the
# 2 kHz row above is a false NEGATIVE, not a narrowed gap: this speaker
# crosses over at 2 kHz, so the woofer's own passband sits inside the
# analysed band). Disclosure alone does not keep a session inside a
# calibrated regime; the clamp does, and the disclosure keeps the declared
# value visible so nobody has to read the clamped band as a declaration.
# The two quantities the derivation had been conflating are the driver's
# declared operating/measurement WINDOW (excitation + SNR scoring, which
# measurement_band_hz owns) and the echo/null ANALYSIS band (a
# detector-calibration concern, which this floor owns).
#
# **Clamping costs no cross-session comparability**, which is why it is
# cheap: the detector's quefrency step is 1e6 / BANDWIDTH, so the clamped
# JTS3 band (4000, 18000) resolves at 1e6 / 14000 = 71.4 us -- identical to
# the module default (5000, 19000), also 14 kHz wide, the band S0 was
# measured at. A clamped session's tau ladder is directly comparable to
# S0's rather than merely adjacent to it.
#
# See _derive_cloud_echo_band_hz.
ECHO_BAND_HF_REGIME_FLOOR_HZ = 4000.0

# Cloud curves decimated for persistence (bundle cloud.json + the durable v2
# state's compact cloud block) -- mirrors
# jasper.web.correction_crossover_v2.MAX_PERSISTED_SUM_POINTS (512), which
# this module cannot import without a circular dependency (that module
# imports THIS one). Kept as an independent constant rather than a shared one
# for that reason; if the two ever need to diverge, they now can.
CLOUD_CURVE_MAX_JSON_POINTS = 512


def _composed_swept_band_hz(roles: Sequence[RoleBand]) -> tuple[float, float]:
    """The summed system's swept band -- the union of every declared
    ``RoleBand.band`` -- PR-4's contract-derived ``signal_band_hz``.

    No existing function composes across roles (each ``RoleBand.band`` is one
    driver's own excitation-ceiling band, from
    ``excitation_safety_plan.resolve_driver_excitation_ceilings``); this is
    that composition, added here because it is conductor-owned wiring policy
    (which roles participate in the passband), not a pure-DSP concern that
    belongs in ``spatial_combine`` or ``program.py``.
    """
    lo = min(float(r.band.lower_hz) for r in roles)
    hi = max(float(r.band.upper_hz) for r in roles)
    return (lo, hi)


@dataclass(frozen=True)
class _CloudEchoBand:
    """The echo/null analysis band the pipeline will APPLY, plus how it was
    derived -- one value, so the band and its provenance cannot be carried
    (or persisted) apart from each other.

    ``band_hz`` is what the detector actually runs on. ``derived_lo_hz`` is
    the lower edge the declared contract produced BEFORE the HF-regime clamp
    (equal to ``band_hz[0]`` whenever no clamp happened), so a reader can
    always tell a contract-derived band from a clamped one **without** the
    journal -- the honesty rule issue #1763 turned into a requirement.
    ``source`` names WHICH derivation path produced the band, because
    "the module default" means something different when nothing was declared
    than when a clamp could not produce a usable band:

    * ``declared`` -- the tweeter's declared ``measurement_band_hz``,
      possibly narrowed by the passband containment clamp, possibly raised
      by the HF-regime clamp (``hf_regime_clamped`` tells which).
    * ``undeclared_default`` -- no measurement band was threaded through, so
      ``DEFAULT_ECHO_BAND_HZ`` stands in (pre-PR-4 behaviour, unchanged).
    * ``clamp_degenerate_default`` -- the HF clamp would have left a band too
      narrow for the detector to resolve anything in (see
      :func:`_min_clamped_echo_band_width_hz`), so ``DEFAULT_ECHO_BAND_HZ``
      stands in instead.
    * ``passband_fallback`` -- the declared band sits entirely outside the
      composed passband, so the passband itself stands in.
    """

    band_hz: tuple[float, float]
    source: str
    hf_regime_clamped: bool
    derived_lo_hz: float

    def disclosure(self) -> dict[str, Any]:
        """The JSON-native provenance block the pipeline payload carries.

        Deliberately does NOT repeat ``band_hz``: the payload already
        publishes the applied band as ``echo_band_hz``, and two copies of one
        pair is how they come to disagree.
        """
        return {
            "source": self.source,
            "hf_regime_clamped": self.hf_regime_clamped,
            "derived_lo_hz": float(self.derived_lo_hz),
            "floor_hz": ECHO_BAND_HF_REGIME_FLOOR_HZ,
        }


def _min_clamped_echo_band_width_hz() -> float:
    """The narrowest band the HF-regime clamp may hand the detector, derived
    from the DETECTOR's own constants rather than picked.

    ``detect_echo``'s quefrency step is ``resolution_us = 1e6 / bandwidth``,
    and two of its gates are multiples of that step: the searched window's
    edge margin (``WINDOW_EDGE_MARGIN_STEPS``, one step above
    ``search_us[0]``) and -- independently of the window --
    ``assess_geometry``'s refusal to cluster any estimate whose ``tau_us``
    is below ``GEOMETRY_MIN_RESOLUTION_STEPS * resolution_us``. The geometry
    floor is the binding one, and once it reaches the TOP of the searched
    window no delay the detector is allowed to look for can be clustered at
    all, so the band cannot produce a geometry lock however good the room is:

        GEOMETRY_MIN_RESOLUTION_STEPS * 1e6 / DEFAULT_ECHO_SEARCH_US[1]
          = 3.0 * 1e6 / 800 us
          = 3750 Hz

    (The edge margin's own bound is 1.0 * 1e6 / (800 - 120) us => 1470 Hz,
    i.e. slacker, which is why the geometry floor is the one to read.
    ``DEFAULT_ECHO_SEARCH_US`` is the right window to read because this
    program's ``combine_positions`` call passes no ``echo_search_us``, so the
    default window is the one actually searched.)

    This dominates the detector's other width constraint,
    ``MIN_ECHO_BAND_BINS`` (16 bins of ``detect_echo``'s own FFT): that FFT
    is floored at 4096 points, so at this program's 48 kHz the coarsest bin
    spacing is 11.72 Hz and 16 bins need only 15 * 11.72 = 175.8 Hz -- 21x
    narrower than the bound above. One rule is therefore enough: a band that
    clears this floor clears the bin-count refusal too.

    Derived rather than hard-coded so a change to either detector constant
    moves this bound with it instead of leaving a stale literal behind.
    """
    from jasper.audio_measurement.spatial_combine import (
        DEFAULT_ECHO_SEARCH_US,
        GEOMETRY_MIN_RESOLUTION_STEPS,
    )

    return GEOMETRY_MIN_RESOLUTION_STEPS * 1e6 / float(DEFAULT_ECHO_SEARCH_US[1])


def _derive_cloud_echo_band_hz(
    signal_band_hz: tuple[float, float],
    tweeter_measurement_band_hz: tuple[float, float] | None,
) -> _CloudEchoBand:
    """The contract-derived echo/null analysis band (PR-4): the tweeter's
    declared ``measurement_band_hz``, replacing ``DEFAULT_ECHO_BAND_HZ``'s
    flat constant at this call site -- returned WITH its provenance (see
    :class:`_CloudEchoBand`).

    Falls back to ``DEFAULT_ECHO_BAND_HZ`` when the tweeter's measurement
    band was not threaded through (an older/incomplete confirmed profile) --
    that constant is the module's own long-standing default, not a new
    invention, and every existing corpus test that validated
    ``identify_interference_nulls`` against the S0 corpus did so at exactly
    this band (``S0_BAND_HZ`` in ``tests/test_interference_nulls.py``).

    **Containment (inherited PR-2/PR-6a constraint):** clamped to sit INSIDE
    ``signal_band_hz`` (the derived passband), never wider. A band that
    neither contains nor sits clear of the analysis band leaves
    ``detect_echo``'s signal-presence screen uncalibrated
    (``spatial_combine.BAND_BELOW_PASSBAND_MARGIN_DB``'s docstring: "What is
    NOT calibrated: a passband narrower than the analysis band, or
    overlapping it"). Since ``signal_band_hz`` is the union of BOTH roles'
    excitation bands (always at least as wide as one driver's own
    measurement window in the ordinary 2-way case -- the woofer's lower edge
    sits well below the tweeter's, and the tweeter's own excitation ceiling
    upper edge is never narrower than its measurement band, per
    ``resolve_driver_excitation_ceilings``'s "Band-edge asymmetry" rule),
    this clamp is a no-op for every declared contract exercised by this
    program's tests and only bites a genuinely malformed one.

    **HF regime (issue #1763):** when the contained lower edge sits below
    :data:`ECHO_BAND_HF_REGIME_FLOOR_HZ`, it is RAISED to that floor and the
    clamp is disclosed -- a WARNING event (slug suffix
    ``cloud_echo_band_clamped_to_hf_regime``) plus the provenance this
    returns, so neither a journal reader nor a payload reader has to infer
    it from the band alone. The contract's
    upper edge is kept: the floor is a statement about where the detector's
    calibrations hold, not about how wide the driver's window is. See
    :data:`ECHO_BAND_HF_REGIME_FLOOR_HZ`'s own comment for the six-band
    deficit table behind the number, and for why PR-4's disclose-and-proceed
    design was replaced.

    **When the clamp cannot produce a usable band** -- the surviving width
    ``upper - floor`` is below :func:`_min_clamped_echo_band_width_hz` -- the
    band falls back to ``DEFAULT_ECHO_BAND_HZ`` with its own disclosure
    rather than to a stub the detector would refuse everything in. That trade
    is stated rather than glossed: the default is NOT re-clamped into the
    passband, so in this corner the band can sit outside a pathologically low
    passband and leave the signal-presence screen's deficit statistic
    uncalibrated. That is the lesser loss -- an uncontained band still runs
    both estimators, whereas a band too narrow to resolve any delay in the
    searched window makes every number downstream meaningless. It is also
    unreachable from any plausible contract: it needs
    ``min(declared_upper, passband_upper)`` below 7750 Hz, i.e. a "tweeter"
    (or a whole 2-way system) that is not swept into the top three octaves --
    the same malformed-contract family as the passband fallback below.
    """
    from jasper.audio_measurement.spatial_combine import DEFAULT_ECHO_BAND_HZ

    declared = tweeter_measurement_band_hz is not None
    band = tweeter_measurement_band_hz or DEFAULT_ECHO_BAND_HZ
    lo = max(float(band[0]), float(signal_band_hz[0]))
    hi = min(float(band[1]), float(signal_band_hz[1]))
    if lo >= hi:
        # A genuinely malformed declared contract -- the tweeter's own
        # measurement band sits entirely outside the composed passband.
        # Fall back to the passband itself rather than hand a caller an
        # inverted/degenerate pair that would raise deep inside
        # combine_positions with no context about why.
        log_event(
            logger, "correction.crossover_v2_cloud_echo_band_degenerate",
            level=logging.WARNING,
            declared_measurement_band_hz=list(band),
            signal_band_hz=list(signal_band_hz),
        )
        return _CloudEchoBand(
            band_hz=(float(signal_band_hz[0]), float(signal_band_hz[1])),
            source="passband_fallback",
            hf_regime_clamped=False,
            derived_lo_hz=lo,
        )
    if lo < ECHO_BAND_HF_REGIME_FLOOR_HZ:
        min_width_hz = _min_clamped_echo_band_width_hz()
        if hi - ECHO_BAND_HF_REGIME_FLOOR_HZ < min_width_hz:
            log_event(
                logger, "correction.crossover_v2_cloud_echo_band_clamp_degenerate",
                level=logging.WARNING,
                derived_lo_hz=lo, upper_hz=hi,
                floor_hz=ECHO_BAND_HF_REGIME_FLOOR_HZ,
                min_width_hz=min_width_hz,
                fallback_band_hz=list(DEFAULT_ECHO_BAND_HZ),
            )
            return _CloudEchoBand(
                band_hz=(float(DEFAULT_ECHO_BAND_HZ[0]), float(DEFAULT_ECHO_BAND_HZ[1])),
                source="clamp_degenerate_default",
                hf_regime_clamped=False,
                derived_lo_hz=lo,
            )
        # ``clamped_lo_hz`` equals ``floor_hz`` by construction; both are
        # logged so a journal reader does not have to know that to read the
        # line.
        log_event(
            logger, "correction.crossover_v2_cloud_echo_band_clamped_to_hf_regime",
            level=logging.WARNING,
            derived_lo_hz=lo, clamped_lo_hz=ECHO_BAND_HF_REGIME_FLOOR_HZ,
            floor_hz=ECHO_BAND_HF_REGIME_FLOOR_HZ, upper_hz=hi,
        )
        return _CloudEchoBand(
            band_hz=(ECHO_BAND_HF_REGIME_FLOOR_HZ, hi),
            source="declared" if declared else "undeclared_default",
            hf_regime_clamped=True,
            derived_lo_hz=lo,
        )
    return _CloudEchoBand(
        band_hz=(lo, hi),
        source="declared" if declared else "undeclared_default",
        hf_regime_clamped=False,
        derived_lo_hz=lo,
    )


def _decimate_curve_for_json(
    freqs_hz: np.ndarray, magnitude_db: np.ndarray,
) -> dict[str, list[float]]:
    """Stride-decimate one combined curve to at most
    :data:`CLOUD_CURVE_MAX_JSON_POINTS`, for disclosure only -- mirrors
    ``jasper.web.correction_crossover_v2._decimate_sum``'s exact shape
    (floor-division stride, identity when already short enough) so the two
    persisted curve payloads (VERIFY's predicted sum, the cloud's combined
    spec curve) read the same way to a consumer.
    """
    n = len(freqs_hz)
    step = max(1, n // CLOUD_CURVE_MAX_JSON_POINTS)
    return {
        "freqs_hz": [float(f) for f in freqs_hz[::step]],
        "magnitude_db": [float(m) for m in magnitude_db[::step]],
    }


def _null_registry_to_dict(report: Any) -> dict[str, Any]:
    """``InterferenceNullReport`` -> a plain JSON dict.

    PR-1 shipped no ``to_dict`` (the module docstring's own words: "zero
    production callers by design until the plan's PR-4 wires it into the
    conductor's cloud-group analysis") -- this is that wiring layer's owned
    serialization, mirroring ``FlatSpecReport.to_dict``'s shape so the two
    persisted reports read consistently.
    """
    return {
        "nulls": [
            {
                "f_lo_hz": n.f_lo_hz, "f_hi_hz": n.f_hi_hz,
                "f_center_hz": n.f_center_hz, "n": n.n, "tau_us": n.tau_us,
                "r_time": n.r_time, "r_freq": n.r_freq,
                "agreement": n.agreement, "depth_db": n.depth_db,
                "classification": n.classification,
                "evidence": dict(n.evidence),
            }
            for n in report.nulls
        ],
        "excluded_bands_hz": [list(b) for b in report.excluded_bands_hz],
        "excluded_fraction": float(report.excluded_fraction),
        "refusals": [
            {
                "f_center_hz": r.f_center_hz, "depth_db": r.depth_db,
                "reason": r.reason, "evidence": dict(r.evidence),
            }
            for r in report.refusals
        ],
        "reason": report.reason,
        "classification": report.classification,
        "band_hz": list(report.band_hz),
        "tau_ladder_us": float(report.tau_ladder_us),
        "arrival_tau_us": float(report.arrival_tau_us),
        "arrival_r_time": float(report.arrival_r_time),
        "arrival_r_max": float(report.arrival_r_max),
        "n_corroborating": int(report.n_corroborating),
        "r_freq": float(report.r_freq),
        "agreement": float(report.agreement),
        "ladder_arrival_gap": float(report.ladder_arrival_gap),
        "capped": bool(report.capped),
        "min_depth_db": float(report.min_depth_db),
        "n_candidates": int(report.n_candidates),
    }


def _geometry_guidance_copy(geometry: Mapping[str, Any]) -> str:
    """Plain-language "spread the mic further" guidance from a geometry
    verdict dict (:func:`cloud_geometry_verdict`'s own shape) -- the
    household-facing surface issue #1742 item 2 asked for. Recorded since
    PR-3b (the durable v2 state's ``cloud`` block, ``GEOMETRY_RETRY_POSITIONS``'s
    own comment). PR-4 carries this copy onto the envelope and `/state`
    (`crossover_v2_status_block`'s compact projection); no household-facing
    surface renders it yet (zero JS/asset changes in PR-4) -- PR-7 renders
    it.

    Softened, never suppressed, when ``thin_evidence`` -- and the softened
    copy names the qualitative floor ("the bare minimum of positions"),
    never a discrete number or a percentage, because thin_evidence is a
    cliff at an exact confident-estimate count, not a gradient
    (spatial_combine.GeometryLock's own docstring) -- naming the actual
    count would read as a gradient the instrument does not claim. Empty
    string when not locked -- nothing to say.
    """
    if not geometry.get("locked"):
        return ""
    if geometry.get("thin_evidence"):
        return (
            "The measured echo pattern looks the same at every microphone "
            "position, but only the bare minimum of positions gave a "
            "confident enough reading to tell. Spreading the microphone "
            "further apart next time would make this more certain."
        )
    return (
        "The measured echo pattern did not change between microphone "
        "positions. Spreading the microphone further apart next time may "
        "help JTS tell the speaker's own sound apart from the room's."
    )


# --------------------------------------------------------------------------- #
# Carve-out disclosure (owner decision 1, 2026-07-25; plan PR-6b)
#
# The owner's decision of record: identified interference nulls are excluded
# from spec evaluation AND from correction, the band's tolerance applies to the
# SURVIVING envelope, and "the report discloses 'EQ cannot fill these' with the
# numbers." ``evaluate_flat_spec`` already does the excluding -- the masked bins
# leave both the reference level and every band's deviation. What it does not
# do, and must not, is say WHY: it is a pure evaluator that takes a bool mask
# and holds no product policy (its own module docstring). So the "why" is
# assembled here, in the wiring layer that already holds the registry and the
# spec report side by side, next to ``_geometry_guidance_copy`` -- the other
# household-facing copy derived from a pipeline verdict.
#
# **This module owns the carve-out copy strings; PR-7 renders them.** One
# owner, so a chart callout and the envelope's expert disclosure cannot say
# different things about the same carved range.
# --------------------------------------------------------------------------- #

# Which honesty instrument carved a range. Snake_case and self-identifying,
# mirroring the vocabulary rule interference_nulls.py states for its own slugs.
CARVE_OUT_SOURCE_IDENTIFIED_NULL = "identified_null"
CARVE_OUT_SOURCE_POSITION_SCREEN = "position_screen"


def _format_carve_out_hz(hz: float) -> str:
    """One frequency as household copy — kHz at and above 1 kHz, Hz below.

    Deliberately NOT the ``f"{hz:.0f} Hz"`` form the envelope's flatness lines
    use: those quote a single worst bin, while these copy strings list several
    frequencies in one sentence, where five-digit Hz figures read as noise.
    """
    return f"{hz / 1000.0:.1f} kHz" if hz >= 1000.0 else f"{hz:.0f} Hz"


def _join_carve_out_phrases(parts: Sequence[str]) -> str:
    """``["a", "b", "c"]`` -> ``"a, b and c"``. No serial comma, matching the
    house copy elsewhere in this flow."""
    parts = tuple(parts)
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def _null_classification_copy(classification: str) -> str:
    """The classification's own household sentence, or ``""`` for a
    classification this copy does not cover.

    **The ``position_invariant`` wording is load-bearing and pre-registered**
    (plan PR-1's classification vocabulary, PR-7's callout copy): a single
    session cannot separate "travels with the speaker" from "a path in the room
    that did not change while measuring", and the output must not claim it can
    — S0 separated them only by MOVING the speaker. So the copy names both and
    names the experiment that would tell them apart.

    No hardware noun appears here, in either branch. The classification is
    evidence about how a null behaved across a mic cloud; it is not evidence
    about what part of a speaker or room produced it, and naming one would be
    the device-taxonomy guess this program forbids in shipped copy (the JTS3
    rim-wave attribution is session knowledge, not measured general truth).
    """
    from jasper.audio_measurement.interference_nulls import (
        CLASSIFICATION_POSITION_DEPENDENT,
        CLASSIFICATION_POSITION_INVARIANT,
    )

    if classification == CLASSIFICATION_POSITION_INVARIANT:
        return (
            " It sat at the same frequencies at every microphone position — "
            "consistent with something that travels with the speaker, or with "
            "a path that did not change while measuring; moving the speaker "
            "and measuring again would tell those apart."
        )
    if classification == CLASSIFICATION_POSITION_DEPENDENT:
        return (
            " It appeared at some microphone positions and not others, so "
            "whatever causes it does not travel with the speaker."
        )
    return ""


def _carve_out_records(
    null_report: Any, screen_bands_hz: Sequence[Sequence[float]],
) -> list[dict[str, Any]]:
    """Every carved range, tagged with the instrument that carved it.

    The two honesty instruments are listed SEPARATELY rather than merged: a
    merged interval loses which instrument found it, and the registry's rows
    are the only ones carrying τ/r — the exclusion *reason of record*. Ranges
    from the two sources may overlap each other; that is reported as two rows
    (one per instrument's own evidence), not silently collapsed, because "both
    instruments flagged this" is a stronger statement than either alone.
    ``merged_excluded_bands_hz`` remains the merged view for anyone counting.

    A registry row's interval is the null's OWN ``f_lo_hz``/``f_hi_hz`` (its
    half-depth width), unclipped to any spec band — τ and r describe the whole
    null, so clipping the interval to a band edge would attach the numbers to a
    fragment of what was measured.

    Ordered by lower edge, then by source, so two rows starting at the same
    frequency come out in a stable order rather than an input-order one.
    """
    records: list[dict[str, Any]] = []
    for null in null_report.nulls:
        records.append(
            {
                "f_lo_hz": float(null.f_lo_hz),
                "f_hi_hz": float(null.f_hi_hz),
                "source": CARVE_OUT_SOURCE_IDENTIFIED_NULL,
                "f_center_hz": float(null.f_center_hz),
                "n": int(null.n),
                "tau_us": float(null.tau_us),
                "r_time": float(null.r_time),
                "r_freq": float(null.r_freq),
                "depth_db": float(null.depth_db),
                "classification": str(null.classification),
                "reason": (
                    "A delayed copy of the sound cancels this range, and EQ "
                    "cannot fill a cancellation, so it is left out of "
                    "correction and out of grading."
                    + _null_classification_copy(str(null.classification))
                ),
            }
        )
    for band in screen_bands_hz:
        records.append(
            {
                "f_lo_hz": float(band[0]),
                "f_hi_hz": float(band[1]),
                "source": CARVE_OUT_SOURCE_POSITION_SCREEN,
                "reason": (
                    "The microphone positions disagreed about this range much "
                    "more than about the rest of the spectrum, so it reads as "
                    "interference rather than the speaker's own response and "
                    "is left out of correction and out of grading."
                ),
            }
        )
    records.sort(key=lambda record: (record["f_lo_hz"], record["source"]))
    return records


def _carve_out_disclosure_copy(records: Sequence[Mapping[str, Any]]) -> str:
    """The band's household-facing headline — plain language, no τ/r.

    ``""`` when nothing was carved in the band, mirroring
    :func:`_geometry_guidance_copy`'s "empty string when not locked — nothing
    to say" rule rather than rendering a "no interference found" sentence a
    reader could mistake for a measurement.

    The delay is quoted in **milliseconds** here because it is the one number
    that makes the sentence mean something to a household ("a delayed copy
    arrives 0.32 ms later"); τ stays in microseconds in the structured record,
    which is the registry's own unit and the one owner of it.
    """
    nulls = [r for r in records if r["source"] == CARVE_OUT_SOURCE_IDENTIFIED_NULL]
    screened = [r for r in records if r["source"] == CARVE_OUT_SOURCE_POSITION_SCREEN]
    sentences: list[str] = []
    if nulls:
        where = _join_carve_out_phrases(
            [_format_carve_out_hz(float(r["f_center_hz"])) for r in nulls]
        )
        # One ladder, one τ (IdentifiedNull.tau_us is "the same value on every
        # rung of one report" — its own docstring), so the first row's delay
        # describes them all.
        delay_ms = float(nulls[0]["tau_us"]) / 1000.0
        plural = len(nulls) > 1
        sentences.append(
            f"{'Interference nulls at' if plural else 'An interference null at'} "
            f"{where} — a delayed copy of the sound arrives {delay_ms:.2f} ms "
            f"later. EQ cannot fill {'these' if plural else 'this'}, so "
            f"{'they are' if plural else 'it is'} left out of correction and "
            "out of this band's grading."
        )
    if screened:
        plural = len(screened) > 1
        # "One range" rather than "1 range": this is prose, and the frequency
        # figures are the numerals a reader should be counting in it.
        count = f"{len(screened)}" if plural else "One"
        subject = f"{count} {'further ' if nulls else ''}"
        subject += "ranges are" if plural else "range is"
        tail = (
            "left out because the microphone positions disagreed about "
            if nulls
            else (
                "left out of correction and out of this band's grading "
                "because the microphone positions disagreed about "
            )
        )
        sentences.append(
            f"{subject} {tail}{'them' if plural else 'it'} too much to grade."
        )
    return " ".join(sentences)


def _carve_out_expert_copy(records: Sequence[Mapping[str, Any]]) -> str:
    """The expert-layer line — the same carve-outs WITH τ and r.

    Separated from :func:`_carve_out_disclosure_copy` rather than folded into
    it because the two registers have different readers and the plan puts τ/r
    behind a disclosure ("τ/r vocabulary lives in an expert disclosure, not the
    headline"). Both are produced here so a chart callout and the envelope's
    ``<details>`` cannot drift into saying different things.

    ``r`` is reported as the pair the registry actually holds — the
    time-domain and frequency-domain estimates — rather than one averaged
    figure, because their AGREEMENT is what admitted the null in the first
    place, and an average would hide it.
    """
    nulls = [r for r in records if r["source"] == CARVE_OUT_SOURCE_IDENTIFIED_NULL]
    if not nulls:
        return ""
    where = _join_carve_out_phrases(
        [
            f"{_format_carve_out_hz(float(r['f_center_hz']))} (rung {int(r['n'])}, "
            f"{float(r['depth_db']):.1f} dB deep)"
            for r in nulls
        ]
    )
    first = nulls[0]
    return (
        f"carved out of grading: {where}; delay τ {float(first['tau_us']):.0f} µs, "
        f"reflection ratio r {float(first['r_time']):.3f} measured in time / "
        f"{float(first['r_freq']):.3f} implied by null depth"
    )


def carve_outs_by_band(
    spec_report: Any,
    null_report: Any,
    screen_bands_hz: Sequence[Sequence[float]],
) -> list[dict[str, Any]]:
    """Per spec band: which ranges were carved out, why, and with what numbers.

    Owner decision 1 (2026-07-25) in payload form. One entry per band of
    ``spec_report``, **always all of them, in the report's own order**, so a
    consumer can join to ``spec["bands"]`` by index or by ``band_hz`` and can
    render "nothing carved here" without having to infer it from an absence.

    A record is included in a band when its interval OVERLAPS the band's
    ``[f_lo_hz, f_hi_hz)`` span, so a null straddling a band edge appears under
    both bands it actually carves — it removes bins from both.

    **What this does NOT include: the gate-validity clamp.** Bins below the
    group's ``validity_floor_hz`` also leave the spec evaluation (plan PR-5),
    but they are not an interference verdict and PR-5 deliberately keeps them
    out of the honesty instruments' own accounting, disclosed separately as
    ``validity_floor_hz``. So a band's ``n_excluded`` on the spec report can
    exceed what these records cover, and the floor is the difference — the same
    separation ``_compact_cloud_status`` carries for exactly this reason.
    """
    records = _carve_out_records(null_report, screen_bands_hz)
    out: list[dict[str, Any]] = []
    for band in spec_report.bands:
        f_lo, f_hi = float(band.f_lo_hz), float(band.f_hi_hz)
        in_band = [
            record
            for record in records
            if record["f_lo_hz"] < f_hi and record["f_hi_hz"] > f_lo
        ]
        out.append(
            {
                "band_hz": [f_lo, f_hi],
                "intervals": [dict(record) for record in in_band],
                "disclosure": _carve_out_disclosure_copy(in_band),
                "expert": _carve_out_expert_copy(in_band),
            }
        )
    return out


@dataclass(frozen=True)
class _CloudFitEvidence:
    """What a closed spatial cloud contributes to the correction envelope.

    The three optional arguments of
    :func:`~jasper.active_speaker.linearization_envelope.compose_envelope`,
    travelling together as one value so the fit cannot be handed a half-supplied
    pair (``compose_envelope`` raises on ``band_spread`` without
    ``n_positions``, and this makes that unreachable from this module).

    ``excluded_bands_hz`` is the MERGED honesty mask — the power-vs-median
    screen union the identified-null registry, as
    :func:`assemble_cloud_group_result` merged it. Not the screen's intervals
    and not the registry's: the wiring contract (issue #1742 item 4) is that
    the instruments are consumed together.
    """

    excluded_bands_hz: tuple[tuple[float, float], ...]
    band_spread: tuple[Any, ...]
    n_positions: int


def cloud_validity_floor_hz(positions: Sequence[_CloudPosition]) -> float | None:
    """The group's own gated validity floor — the WORST (highest) of its
    positions' floors, or ``None`` when no position reported a usable one.

    Why the worst rather than a mean or the anchor's: the combined curve is a
    power mean ACROSS these positions, so a bin below any one position's
    reflection-gate floor is contaminated in the average by that position's
    truncated-window artifact (``gating.f_valid_floor_hz`` — the same
    quantity ``_analyze_verify``'s tracking band already clamps up to, W6.9
    forensics). Taking the highest floor is the only choice under which every
    graded bin is inside every contributing capture's validity.

    ``None`` (no position carried a finite, positive floor) means the lower
    edge could not be verified — NOT that it is zero. Callers disclose it as
    unknown and clamp nothing; see :func:`assemble_cloud_group_result`.
    """
    floors = [
        float(getattr(p.response, "validity_floor_hz", None) or 0.0)
        for p in positions
    ]
    usable = [f for f in floors if math.isfinite(f) and f > 0.0]
    return max(usable) if usable else None


def assemble_cloud_group_result(
    combined: Any,
    *,
    echo_band_hz: tuple[float, float],
    echo_band_provenance: Mapping[str, Any] | None = None,
    validity_floor_hz: float | None = None,
    tier: str = "",
) -> dict[str, Any]:
    """The wiring contract (issue #1742 item 4) -- THE single function that
    consumes the exclusion mask, ``geometry.locked``, and the null registry
    TOGETHER. No other code in this program may read
    ``combined.excluded``/``combined.geometry.locked`` and treat that as the
    honesty verdict on its own; doing so is reading the mask alone, the hole
    this item exists to close (see the plan doc's "Architecture" table: "the
    mask alone is a hole").

    Runs :func:`~jasper.audio_measurement.interference_nulls.identify_interference_nulls`
    on ``combined`` at ``echo_band_hz``, unions its excluded bins with the
    combiner's own power-vs-median screen (``combined.excluded``), and
    evaluates :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec`
    against the merged mask -- the plan's "merged honesty mask = screen ∪
    identified nulls" line, made executable.

    ``combined`` may be ``None`` (the group could not be combined at all --
    :func:`combine_cloud_positions`'s own honest "unknown") or a
    :class:`~jasper.audio_measurement.spatial_combine.CombinedResponse`.

    **The spec-curve SSOT (plan PR-5).** The ``spec`` report this builds is
    the ONE construction every spec-facing surface reads -- the flatness
    gauge, the observe ledger's spec-facing summary, `/state`, and the
    envelope all render ``flatness`` (:func:`~jasper.active_speaker.flat_spec.spec_flatness_gauge`
    of that same report) rather than deriving a number of their own. Nothing
    downstream re-evaluates the curve.

    **The carve-out disclosure (plan PR-6b, owner decision 1).** ``carve_outs``
    is :func:`carve_outs_by_band` of the SAME registry and the SAME spec report
    — per band, which ranges left this band's grading, in plain language, with
    τ/r behind an expert string. It is a third reading of one evaluation, never
    a second one: the bins are already gone from ``spec`` by the time this runs,
    and no verdict here can move. The tolerance table is untouched — the 8-16 kHz
    row still reads ±2.5 dB, applied to whatever survives the carve-out (the
    owner's decision was to disclose the carve-out, not to re-spec the band).

    **``echo_band_provenance`` (issue #1763) is how a payload reader tells a
    contract-derived band from a clamped one.** ``echo_band_hz`` publishes the
    band the detector actually ran on, which is necessary but not sufficient:
    a reader seeing ``[4000, 18000]`` cannot tell whether the driver declared
    that window or whether the HF-regime clamp raised a declared 2 kHz edge
    into it, and the difference is exactly the asterisk issue #1763 exists to
    make visible. :meth:`_CloudEchoBand.disclosure` supplies the block (its
    ``source`` / ``hf_regime_clamped`` / ``derived_lo_hz`` / ``floor_hz``);
    the conductor passes it alongside the band it came from. ``None`` when a
    caller did not state one — "not stated", never "not clamped", the same
    unknown-vs-zero rule ``validity_floor_hz`` follows below.

    **``validity_floor_hz`` clamps the spec band's lower edge.** Bins below
    the group's gated validity floor (:func:`cloud_validity_floor_hz`) are
    "not a measurement, they're an artifact of a truncated gate window"
    (``_analyze_verify``'s own W6.9 comment about the tracking band), so they
    are excluded from the spec evaluation -- from the reference level as well
    as from every band's deviation, since a contaminated bin must not be able
    to re-center the target either. Two properties this deliberately keeps:

    * The clamp rides the evaluation's exclusion mask but **not**
      ``merged_excluded_bands_hz``, which stays the honesty instruments'
      own count (screen union identified nulls). ``excluded_interval_count``
      on `/state` is the "how much interference did we find" number and must
      not silently absorb a gate artifact. ``validity_floor_hz`` is reported
      alongside so a reader can tell the two apart in ``spec.n_excluded`` --
      and it is carried all the way to the LIVE surfaces, not just the
      durable state and the bundle: ``_compact_cloud_status`` projects it
      onto `/state`, the envelope, and the doctor's read. Without that a page
      seeing a large ``n_excluded`` could not distinguish a combed room from
      one capture's collapsed gate.
    * A ``None`` floor clamps NOTHING and is reported as ``None``. The
      alternative -- withholding the whole gauge, which is what the retired
      per-capture ``_flatness_tracking`` did when a capture had no floor --
      would throw away the 2-16 kHz evidence over an unverified lower edge.

    Regime, measured on the S0 main leg 2026-07-27 (``test_flat_spec_ssot.py``
    pins every figure below): the spec table's lower edge is 250 Hz and NINE
    of that session's ten positions gate to 142.9 Hz, where the clamp changes
    no graded number at all -- every band figure, the reference level, the
    verdict, and the whole gauge are byte-identical (only the report-wide
    ``excluded_intervals`` gains the sub-250 Hz region it removed, which is
    why the gauge quotes spec-band BIN counts and not an interval count). The
    tenth, ``cloud_04``, collapsed to **1777.8 Hz**, so the group floor is
    1777.8 Hz and the clamp:

    * moves **1009 bins** out of the 250 Hz-2 kHz band;
    * **re-centres the reference** -27.2670 -> -28.3166 dB (-1.0495 dB),
      because the reference is a power mean over non-excluded 250 Hz-8 kHz
      bins and the clamp removed the loud low end of it;
    * moves the HEADLINE ``max_db`` -8.9399 -> -7.8903 dB, i.e. **+1.0495 dB
      in the FLATTERING direction** -- exactly the reference shift, because
      the worst bin (15999.7 Hz) survives the clamp, so its deviation moves
      one-for-one with the reference. This is the first number the ledger
      line prints and it moves FURTHER than the RMS does;
    * takes the pooled RMS 3.7649 -> 3.1524 dB (-0.6125 dB);
    * **flips the 250 Hz-2 kHz band verdict**, +4.1637 dB (fail) ->
      -1.2855 dB (pass), since ``BandResult.passed`` is
      ``abs(max_deviation_db) <= tolerance_db``. Overall stays False here
      only because the other two bands still fail on their own.

    Direction is **response-shape dependent, not a property of the clamp**:
    on THIS corpus the removed region sat above the surviving reference, so
    dropping it lowered the reference and flattered every surviving
    deviation. A speaker whose sub-floor region is quiet would move the other
    way. Do not generalize the sign.

    None of that is the speaker improving -- it is the same speaker graded on
    fewer bins, which is exactly what ``n_bins``/``n_excluded`` on the gauge
    exist to keep visible (``ConvergenceResidual``'s own rule). One collapsed
    gate in a group is therefore expensive by design.

    **Deferred alternative, recorded rather than dismissed:** the honest
    third option is per-position, per-bin validity masking INSIDE
    ``combine_positions`` -- mask each position's contribution below that
    position's OWN floor and combine the survivors, so nine good captures
    keep contributing at 500 Hz instead of one bad one costing the band. It
    is strictly better than a group-wide clamp and is out of scope here only
    because it is a ``spatial_combine`` signature and estimator change (the
    power mean would need per-bin weights), not a wiring one. Revisit
    trigger: a real session where one collapsed gate meaningfully shrinks the
    graded band -- the S0 ``cloud_04`` case above is that evidence already,
    so this is queued on measured grounds, not speculation.

    **Fail-soft, named, not absolute** (S4 review finding, 2026-07-26 --
    corrected from an earlier "any exception is caught" overclaim). Catches
    exactly ``(ValueError, TypeError, IndexError, AttributeError)`` --
    the documented raise surface of every function this calls
    (:func:`~jasper.audio_measurement.interference_nulls.identify_interference_nulls`
    and :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` both
    raise only ``ValueError`` on malformed input;
    :func:`~jasper.audio_measurement.spatial_combine.merged_true_intervals`
    raises ``ValueError`` via ``zip(strict=True)`` on a length mismatch or
    ``IndexError`` on an out-of-bounds index; a malformed/incomplete
    ``combined``-like object raises ``TypeError``/``AttributeError`` reading
    its fields; :func:`carve_outs_by_band` adds no new family -- it reads
    already-built records by keys it set itself, and its only external reads
    are attribute lookups on the two reports and indexing/``float()`` on the
    screen intervals, i.e. ``AttributeError``/``IndexError``/``TypeError``/
    ``ValueError``). ``_run_cloud_pipeline`` relies on exactly this bounded set --
    a downstream DSP failure inside it is diagnostic/disclosure machinery,
    never a capture-accept gate, so this bounded family is caught and
    reported as ``available: False`` rather than surfacing to the caller.
    Any OTHER exception -- ``KeyError``/``RuntimeError``/``OSError`` (none
    observed on this call surface today; would indicate a genuine bug in a
    callee) or ``MemoryError``/``KeyboardInterrupt`` -- propagates
    uncaught, by design: it should reach the caller rather than silently
    become an honest-looking "unavailable".
    """
    if combined is None:
        return {"available": False, "reason": "combine_failed"}
    try:
        from jasper.active_speaker.flat_spec import (
            evaluate_flat_spec,
            spec_flatness_gauge,
        )
        from jasper.audio_measurement.interference_nulls import (
            identify_interference_nulls,
        )
        from jasper.audio_measurement.spatial_combine import merged_true_intervals

        null_report = identify_interference_nulls(combined, band_hz=echo_band_hz)
        merged_mask = np.asarray(combined.excluded, dtype=bool) | np.asarray(
            null_report.excluded, dtype=bool
        )
        # The honesty mask is what the instruments found; the spec mask adds
        # the gate-validity clamp on top (see this function's docstring for
        # why the two stay distinguishable).
        spec_mask = merged_mask
        if validity_floor_hz is not None and math.isfinite(validity_floor_hz):
            spec_mask = merged_mask | (
                np.asarray(combined.freqs_hz, dtype=float) < float(validity_floor_hz)
            )
        spec_report = evaluate_flat_spec(
            combined.freqs_hz, combined.power_mean_spec_db, spec_mask,
        )
        geometry_dict = {
            "locked": bool(combined.geometry.locked),
            "reason": str(combined.geometry.reason),
            "n_confident": int(combined.geometry.n_confident),
            "n_positions": int(combined.geometry.n_positions),
            "median_tau_us": float(combined.geometry.median_tau_us),
            "clustered_fraction": float(combined.geometry.clustered_fraction),
            "thin_evidence": bool(combined.geometry.thin_evidence),
        }
        return {
            "available": True,
            "geometry": geometry_dict,
            "geometry_guidance": _geometry_guidance_copy(geometry_dict),
            "screen_excluded_bands_hz": [
                list(b) for b in combined.excluded_bands_hz
            ],
            "merged_excluded_bands_hz": [
                list(b) for b in merged_true_intervals(combined.freqs_hz, merged_mask)
            ],
            "null_registry": _null_registry_to_dict(null_report),
            "spec": spec_report.to_dict(),
            # PR-6b: owner decision 1's disclosure half — the SAME registry
            # and the SAME spec report above, re-read per band as "what was
            # carved out of this band's grading, and why". Not a second
            # evaluation: `evaluate_flat_spec` already removed these bins, and
            # nothing here can change a verdict.
            "carve_outs": carve_outs_by_band(
                spec_report, null_report, combined.excluded_bands_hz,
            ),
            # PR-5: the spec-facing gauge — a pure reduction of the SAME
            # ``spec`` report above, carried here so no downstream surface
            # has to (or may) derive its own. Byte-identical wherever it is
            # rendered, because there is one number, copied.
            "flatness": spec_flatness_gauge(spec_report).to_dict(),
            "validity_floor_hz": (
                float(validity_floor_hz)
                if validity_floor_hz is not None and math.isfinite(validity_floor_hz)
                else None
            ),
            "echo_band_hz": list(echo_band_hz),
            "echo_band_provenance": (
                dict(echo_band_provenance)
                if isinstance(echo_band_provenance, Mapping)
                else None
            ),
            # WHICH INSTRUMENT measured this group (flow-simplification §1.2).
            # ``None`` means unknown, never a guessed default — same
            # discipline as ``echo_band_provenance`` directly above, and for
            # the same reason: the two tiers make materially different claims,
            # so a reader that cannot tell them apart must say so.
            "tier": str(tier) or None,
            "curve": _decimate_curve_for_json(
                combined.freqs_hz, combined.power_mean_spec_db,
            ),
        }
    except (ValueError, TypeError, IndexError, AttributeError) as exc:
        log_event(
            logger, "correction.crossover_v2_cloud_pipeline_failed",
            level=logging.WARNING, error=str(exc),
        )
        return {"available": False, "reason": "pipeline_failed"}


def spec_report_for_predicted_sum(predicted_sum: Any) -> Any:
    """Grade the PREDICTED post-apply response against the flat spec.

    ``predicted_sum`` is the ``(freqs_hz, magnitude_db)`` pair
    :func:`~jasper.audio_measurement.program_analysis.predicted_branch_sum`
    produces — on the v2 path, rebuilt from the LINEARIZED branches at the
    committed trim, i.e. a model of exactly what the emitted graph will do.
    Returns a :class:`~jasper.active_speaker.flat_spec.FlatSpecReport`, or
    ``None`` when there is no usable prediction to grade (``None`` input, a
    malformed pair, or a curve the evaluator refuses). **``None`` means
    "unknown", never "passed"** — the caller must not read it as permission.

    The two preparation steps are the caller-side half of
    :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec`'s input
    contract, which takes an already-combined, already-1/3-octave-smoothed
    curve and deliberately owns neither operation. Both are done with the SAME
    owners the measured cloud curve went through, so the two reports differ by
    as little as the two curves' provenance allows:

    * block-average onto the shared analysis grid
      (:func:`~jasper.audio_measurement.spatial_combine.decimate_curve_to_analysis_grid`).
      Not an optimization detail — ``smooth_fractional_octave`` is an
      O(bins x window) Python loop whose cost is effectively quadratic in bin
      count, and a raw 512k-point prediction grid takes ~11 s to smooth on a
      laptop, worse on a Pi 5. The confirm seam is a household waiting on an
      apply; ``MAX_ANALYSIS_BINS`` is the bound the combiner already adopted
      for exactly this reason, with its own "why this is not a loss of
      information" argument (the narrowest window here, 1/3-octave at 250 Hz,
      is ~60 Hz wide against ~1.46 Hz spacing).
    * 1/3-octave smooth at the spec fraction, matching the combiner's
      ``power_mean_spec_db``.

    **The frames are still not identical, and that is stated rather than
    hidden.** The measured curve is a spatial power mean over eight in-room
    positions; this one is a two-branch anechoic-ish model at the mark. Both
    are graded by the same evaluator against the same absolute tolerances and
    both are normalized to their OWN 250 Hz-8 kHz reference, so what survives
    the comparison is SHAPE — which is what the spec grades. It is a coarse
    direction check, and the threshold its caller applies is sized to that.
    """
    if predicted_sum is None:
        return None
    from jasper.active_speaker.flat_spec import evaluate_flat_spec
    from jasper.audio_measurement.analysis import smooth_fractional_octave
    from jasper.audio_measurement.spatial_combine import (
        decimate_curve_to_analysis_grid,
    )

    try:
        freqs_hz, magnitude_db = predicted_sum
        grid, curve_db = decimate_curve_to_analysis_grid(
            np.asarray(freqs_hz, dtype=float), np.asarray(magnitude_db, dtype=float),
        )
        return evaluate_flat_spec(
            grid, smooth_fractional_octave(grid, curve_db, fraction=3),
        )
    except (ValueError, TypeError, IndexError, AttributeError) as exc:
        # The same bounded family, for the same reason, as
        # ``assemble_cloud_group_result``: a malformed or degenerate prediction
        # is a diagnostic gap, not a crash. It becomes an honest "no report",
        # and the caller's own gate decides what an absent report permits.
        log_event(
            logger, "correction.crossover_v2_predicted_spec_failed",
            level=logging.WARNING, error=str(exc),
        )
        return None


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
        tier: str = "",
        driver_spacing_m: float = 0.0,
        accepted_phases: Sequence[str] = (),
        applied: bool = False,
        gain_plan_db: Mapping[str, float] | None = None,
        index_phase_map: Mapping[int, str] | None = None,
        measure_predicted_sum: Any = None,
        measure_gate_window_ms: float | None = None,
        verify_pilot_transfer_baseline: Mapping[str, float] | None = None,
        driver_class_by_role: Mapping[str, str] | None = None,
        tweeter_measurement_band_hz: tuple[float, float] | None = None,
    ) -> None:
        roles = tuple(roles_bands)
        if len(roles) != 2:
            raise CrossoverV2FlowError("the v2 conductor is a 2-way flow")
        self.session_id = str(session_id)
        # Which INSTRUMENT this session is running. Empty = unknown (a caller
        # that never declared one), never silently ``TIER_FULL`` — see
        # ``V2ConductorSnapshot.tier`` for why guessing is the dishonest
        # option. Validated so an unknown id fails at construction rather than
        # riding into the durable state and out to `/state`.
        self._tier = normalize_tier(tier) if tier else ""
        self._preset = source_preset
        self._roles = roles
        self._woofer, self._tweeter = roles[0], roles[1]
        self._fc_hz = float(fc_hz)
        # PR-4: the contract-derived analysis bands for the cloud-group
        # honesty pipeline (combine's echo/signal bands, the null gate's
        # search band) -- computed once here so every group-close event uses
        # the SAME derived values. See _composed_swept_band_hz /
        # _derive_cloud_echo_band_hz for the derivation and their citations.
        self._cloud_signal_band_hz = _composed_swept_band_hz(roles)
        # The band AND its provenance travel as one value (issue #1763), so
        # the pipeline payload can never publish an applied band without the
        # disclosure of how it was derived.
        self._cloud_echo_band = _derive_cloud_echo_band_hz(
            self._cloud_signal_band_hz, tweeter_measurement_band_hz,
        )
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
        # The ordered phases THIS session runs, and — for the position groups —
        # which indexes each spans. Both derive from the map above so a session
        # can never walk a phase it has no capture for (the verify-only re-arm
        # would otherwise sit forever "pending" on a cloud group it never runs).
        present = set(self._index_phase_map.values())
        self._phases = tuple(p for p in CAPTURE_PHASES if p in present)
        self._group_indexes: dict[str, tuple[int, ...]] = {
            phase: tuple(
                sorted(i for i, p in self._index_phase_map.items() if p == phase)
            )
            for phase in self._phases
            if phase in GROUP_PHASES
        }
        # Per-group progress. ``_accepted`` still holds PHASES (one entry per
        # group, added when the group CLOSES); this holds the accepted indexes
        # inside an open group, so accepting position 3 of 8 does not read as
        # "the pre-apply cloud is done."
        self._group_accepted: dict[str, set[int]] = {
            phase: set() for phase in self._group_indexes
        }
        # Retained per-position evidence, in capture order, keyed by group
        # phase. The ASSEMBLY SEAM for PR-4: this list is the input
        # ``combine_positions`` consumes, and PR-4 extends the pipeline that
        # reads it (nulls → spec → persistence) without changing what PR-3b
        # puts in it. Bounded by the plan's own entry count.
        self._group_positions: dict[str, list[_CloudPosition]] = {
            phase: [] for phase in self._group_indexes
        }
        # Geometry-locked retakes already spent, per group — the bound behind
        # "up to GEOMETRY_RETRY_POSITIONS extra positions, ONCE".
        self._geometry_retries_used: dict[str, int] = {
            phase: 0 for phase in self._group_indexes
        }
        # The group's closing geometry verdict, as a plain dict for the host to
        # persist/disclose. ``None`` until the group closes.
        self._group_geometry: dict[str, dict[str, Any]] = {}
        # PR-4: the group's closing honest-instrument pipeline result (mask ∪
        # null registry, evaluated spec, geometry guidance copy) -- see
        # assemble_cloud_group_result. Populated the SAME moment as
        # ``_group_geometry`` above, in ``_close_cloud_group``. ``None`` until
        # the group closes, mirroring that dict's own "never confuse
        # not-yet-run with a clean verdict" rule.
        self._group_cloud_result: dict[str, dict[str, Any]] = {}
        # The group's most recent COMBINE, held from its geometry close until
        # the household confirms past it (flow-simplification §2.6 —
        # ``confirm_cloud_measure_group``). Only CLOUD_MEASURE ever populates
        # it, because only that group's close fits a correction. Held rather
        # than recomputed because a combine is 2.7-6 s of real operator time
        # (see ``_close_cloud_group``); overwritten if a voluntary retake
        # re-closes the group, so the confirm always fits the newest evidence.
        self._group_combined: dict[str, Any] = {}

        # Programs — CHECK is composable now; MEASURE waits on the gain solve,
        # VERIFY on Fc (composable now, played only after apply).
        self._check_program = self._compose_check_program()
        self._measure_program: ExcitationProgram | None = (
            self._compose_measure_program(self._gain_plan_db)
            if self._gain_plan_db is not None
            else None
        )
        self._verify_program = self._compose_verify_program()

        # Per-SLOT attempt bookkeeping + the last failure reason. A slot is the
        # phase for a single-capture phase and the ``phase:index`` pair inside a
        # position group (``_slot_of_index``), so a rejected position spends its
        # own retry budget instead of the whole group's.
        self._phase_attempts: dict[str, int] = {}
        self._last_reason: dict[str, str] = {}
        # Per-slot count of geometry-locked rejections — attempts the conductor
        # spent on GOOD captures to buy spread. Discounted from the slot's
        # failure budget in ``authorize_begin``; see its comment.
        self._geometry_rejections: dict[str, int] = {}
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
        # The accepted MEASURE capture's analysis, held ONLY while something
        # still needs it: from MEASURE's accept until the CLOUD_MEASURE group
        # closes and the fit consumes it (timing move, 2026-07-27 — see
        # ``_measure_verdict``), then released. A session with no cloud group
        # never sets it at all, because that shape fits at MEASURE and consumes
        # the analysis in the same call.
        #
        # **The lifetime is deliberately tight, because the object is not
        # small.** It is dominated by per-occurrence float64/complex128 arrays
        # on the analysis FFT grid, so its size scales with capture length via
        # ``program_analysis._n_fft_for``. Measured 2026-07-27 on the S0
        # corpus's own grid (524,289 bins — a long summed capture): ONE
        # two-occurrence ``DriverResponse`` is 33.6 MB of ndarray payload
        # (4.19 freqs + 4.19 magnitude + 8.39 complex_tf per occurrence). A
        # MEASURE analysis holds one per role with its in-capture repeats
        # attached. That regime is the S0 corpus's, not a production MEASURE's
        # (different program, different grid), and it is quoted to establish
        # the ORDER — tens of megabytes, not kilobytes — on a 1 GB Pi that also
        # retains every cloud position's response for the combine.
        #
        # ``None`` therefore means one of three things, all fine: no MEASURE
        # accepted yet, a session shape that never retains, or an analysis
        # already consumed. Only the FIRST can reach
        # ``_close_measure_cloud_candidate``, and only via a same-session
        # ``hydrate`` — see that method for why production cannot.
        self._measure_analysis: Any = None
        self._candidate: Any = None
        self._verify_outcome: str | None = None  # pass | fail | inconclusive
        # The VERIFY tracking numbers behind the verify_fail screen's collapsed
        # expert disclosure (#1605). Set only once the tolerance comparison is
        # actually reached (the tracking numbers exist); the early-return
        # verdicts (locate/agc/gate/level-shift) leave it None so no half-empty
        # disclosure renders.
        self._verify_evidence: dict[str, Any] | None = None
        # (``_flatness_evidence`` lived here until PR-5. The flatness a
        # household sees is now the cloud-verify group's spec verdict, read
        # off ``group_cloud_result(PHASE_CLOUD_VERIFY)["flatness"]`` — no
        # per-attempt stash, because it is not a per-attempt claim.)
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
        # PR-L4 item 1: the fit's realized inter-driver level verdict, written
        # by ``_fit_linearization`` and asserted by
        # ``_publish_measure_candidate``. ``None`` means no fit ran for this
        # attempt (ineligible / fit_failed), which is NOT a pass — the assertion
        # simply has no linearized branches to grade, exactly as it had none
        # before Layer-1a existed. Same reset discipline as the field above: a
        # prior attempt's verdict must never authorize THIS attempt's candidate.
        self._last_realized_level_match: RealizedLevelMatch | None = None

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

    def _cloud_priors(self) -> MeasurementPriors:
        """Priors for a position-group capture — deliberately WITHOUT
        ``predicted_sum``.

        VERIFY's priors carry the MEASURE-derived prediction so
        ``_analyze_verify`` can compute the tracking comparator ("did apply do
        what the model predicted"). A cloud position must not: the mic is
        OFF the design axis by construction, so measured-vs-predicted
        divergence there is the spatial variation the cloud exists to sample,
        not a tracking error. Withholding the prior leaves
        ``analysis.verify_tracking`` ``None``, so no tracking claim can be
        made from a capture that cannot support one. The flatness/spec claim
        needs no prior at all — since PR-5 it is made ONCE per group, on the
        combined cloud (:func:`assemble_cloud_group_result`), never per
        position.
        """
        return MeasurementPriors(crossover_fc_hz=self._fc_hz)

    # --- read surfaces -------------------------------------------------------

    @property
    def accepted_phases(self) -> frozenset[str]:
        return frozenset(self._accepted)

    def phase_status(self, phase: str) -> str:
        return "accepted" if phase in self._accepted else "pending"

    @property
    def session_phases(self) -> tuple[str, ...]:
        """The ordered phases this session runs (its ``index_phase_map``'s)."""
        return self._phases

    def pending_phases(self) -> tuple[str, ...]:
        return tuple(p for p in self._phases if p not in self._accepted)

    def group_geometry(self, phase: str) -> dict[str, Any] | None:
        """The closing geometry verdict for one position group, or ``None``.

        ``None`` means the group has not closed yet (or this session has no
        such group) — never "the geometry was fine", which is
        ``{"locked": False, ...}``.
        """
        verdict = self._group_geometry.get(phase)
        return dict(verdict) if verdict is not None else None

    def group_cloud_result(self, phase: str) -> dict[str, Any] | None:
        """PR-4's honest-instrument pipeline result for one closed group, or
        ``None`` when the group has not closed yet (mirrors
        :meth:`group_geometry`'s own "never confuse not-yet-run with a clean
        verdict" rule).
        """
        result = self._group_cloud_result.get(phase)
        return dict(result) if result is not None else None

    def group_positions(self, phase: str) -> tuple[str, ...]:
        """Accepted position ids in one group, in capture order."""
        return tuple(p.position_id for p in self._group_positions.get(phase, ()))

    def group_position_takes(self, phase: str) -> tuple[dict[str, Any], ...]:
        """The SURVIVING take per position — ``{position_id, index, attempt}``.

        A position id alone is ambiguous once a geometry retake has happened:
        two takes share it, and only one is in the cloud. The attempt
        disambiguates, and it is what joins these entries to the per-take
        evidence artifacts (which are path-qualified by attempt for exactly
        this reason).
        """
        return tuple(
            {"position_id": p.position_id, "index": p.index, "attempt": p.attempt}
            for p in self._group_positions.get(phase, ())
        )

    @property
    def tier(self) -> str:
        """The commission tier this session runs, or ``""`` when undeclared."""
        return self._tier

    @property
    def current_phase(self) -> str:
        for phase in self._phases:
            if phase not in self._accepted:
                # Everything before VERIFY accepted but not yet applied ⇒ the
                # conductor's own auto-apply is in flight (or has failed) — no
                # human control page, just a brief machine-paced window before
                # VERIFY arms. The MEASURE test is the cheap stand-in for
                # "a candidate should exist by now": on a cloud session the
                # pre-apply group sits between the two and is filtered out by
                # the loop above before this branch is ever reached, so this
                # only fires once the group has closed and the candidate has
                # been built (2026-07-27 timing move).
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

    def _slot_of_index(self, index: int) -> str:
        """The retry-budget key for one capture index.

        For every single-capture phase this is the phase name itself, so the
        CHECK/MEASURE/VERIFY bookkeeping is byte-identical to the pre-cloud
        flow. Inside a position group it is ``phase:index``: eight prompted
        positions are eight independent captures, and collapsing them onto one
        cumulative counter would let a retake at position 2 refuse position 7.
        """
        phase = self._phase_of_index(index)
        return f"{phase}:{index}" if phase in GROUP_PHASES else phase

    def _cloud_prompt(self, phase: str, index: int) -> CloudPositionPrompt:
        """The prompt for one group index — the SAME table the plan emitted.

        A group's first PROMPTED index is its anchor's first move, so the
        group's indexes map onto :data:`CLOUD_POSITION_PROMPTS` from the front:
        the group's ``i``-th index (0-based) takes ``CLOUD_POSITION_PROMPTS[i]``,
        exactly as ``build_v2_capture_plan`` enumerates them. Running off the
        end cannot happen (``_validated_cloud_counts`` refuses a group longer
        than the table), but a defensive fallback keeps a prompt-less capture
        from being a crash rather than a retake.
        """
        offsets = self._group_indexes.get(phase, ())
        try:
            position = offsets.index(index)
        except ValueError:
            position = 0
        if position < len(CLOUD_POSITION_PROMPTS):
            return CLOUD_POSITION_PROMPTS[position]
        return CloudPositionPrompt(
            "Move the phone about a hand-width to a fresh spot you have not "
            "used yet."
        )

    def _prompt_shown_for(self, phase: str, index: int) -> CloudPositionPrompt:
        """The prompt the operator ACTUALLY followed for the take in hand.

        Not always the table entry: after a geometry-locked rejection the phone
        showed a wider-spot retry rung instead, so a retake's evidence must
        record THAT instruction — the sidecar's prompt is the only durable
        statement of where a curve was measured, and one that names a spot the
        operator was told to abandon is worse than none.

        ``_last_reason`` still holds the rejection that produced this retake
        (``consume_capture`` clears it only on acceptance), and
        ``_geometry_retries_used`` counts the rung that was shown, so the pair
        identifies the instruction exactly. A wider-spread rung is ``wide`` by
        construction — it asks for two forearms.
        """
        slot = self._slot_of_index(index)
        if self._last_reason.get(slot) == REASON_CLOUD_GEOMETRY_LOCKED:
            used = max(self._geometry_retries_used.get(phase, 1), 1)
            rung = CLOUD_GEOMETRY_RETRY_PROMPTS[
                min(used - 1, len(CLOUD_GEOMETRY_RETRY_PROMPTS) - 1)
            ]
            return CloudPositionPrompt(rung, wide=True)
        return self._cloud_prompt(phase, index)

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
            session_phases=self._phases,
            applied=self._applied,
            gain_plan_db=dict(self._gain_plan_db) if self._gain_plan_db else None,
            candidate_fingerprint=(
                getattr(self._candidate, "fingerprint", None)
                if self._candidate is not None else None
            ),
            tier=self._tier,
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
            raise CaptureBeginDeferred("awaiting_apply", VERIFY_ANCHOR_HOLD_MESSAGE)
        # Budget: CUMULATIVE per phase by design — the phase's total attempt
        # count is compared against the LAST failure's retry budget, so
        # alternating reason codes cannot restart the meter (a capture that
        # fails `clipped` then `locate_failed` then `clipped`... would retry
        # forever under a literal per-code reading of the §5.10 budget
        # column). This is deliberately stricter than §5.10 read per-code;
        # the plan's `max_attempts` (8) bounds the whole session regardless.
        # First attempt of any slot is always admitted.
        slot = self._slot_of_index(index)
        count = self._phase_attempts.get(slot, 0) + 1
        last = self._last_reason.get(slot)
        # A geometry-locked retake is NOT a quality failure — the capture was
        # good and the conductor asked for a wider one anyway — so it must not
        # eat the slot's failure budget. Without this discount the sequence
        # "geometry retake ×2, then one ordinary bad capture" spends 4 attempts
        # against a 1-retry reason and refuses TERMINALLY, killing a 16-capture
        # session at its last position over a single recoverable glitch. The
        # discount is capped at GEOMETRY_RETRY_POSITIONS so a runaway geometry
        # loop still meets the wall (``_close_cloud_group`` bounds it first;
        # this is the backstop), and the plan's own ``max_attempts`` bounds the
        # whole session regardless.
        forgiven = min(
            self._geometry_rejections.get(slot, 0), GEOMETRY_RETRY_POSITIONS
        )
        if (
            last is not None
            and count - forgiven > REASON_REGISTRY[last].retry_budget + 1
        ):
            spec = REASON_REGISTRY[last]
            raise CaptureBeginRefused(spec.code, spec.message or spec.banner)
        self._phase_attempts[slot] = count
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
        if phase in SUMMED_SWEEP_PHASES:
            # One composed mono summed sweep serves VERIFY and both position
            # groups: identical excitation, identical min-cap clamp, identical
            # ``program.phase`` ("verify") so the analyzer routes it to
            # ``_analyze_verify`` unchanged. What differs between the three is
            # the PRIORS the conductor hands the analysis and the verdict it
            # draws — never the sound the speaker makes.
            return self._verify_program
        raise CrossoverV2FlowError(f"no program for phase {phase!r}")

    def consume_capture(
        self, index: int, attempt: int, result: Any, entry: Any = None,
    ) -> dict[str, Any]:
        """Analyze one uploaded capture and advance (or reject) the phase."""
        phase = self._phase_of_index(index)
        slot = self._slot_of_index(index)
        program = self._program_for_phase(phase)
        priors = (
            self._measure_priors() if phase == PHASE_MEASURE
            else self._verify_priors() if phase == PHASE_VERIFY
            else self._cloud_priors() if phase in GROUP_PHASES
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
        elif phase in GROUP_PHASES:
            verdict = self._consume_cloud_position(
                phase, index, attempt, analysis, result
            )
        else:
            verdict = self._consume_verify(analysis)
        if verdict.accepted:
            # A position group's PHASE is accepted only when its last index is
            # in; a single-capture phase closes on its own acceptance. Both
            # cases route through ``_note_accepted`` so there is one place that
            # decides "this phase is done."
            self._note_accepted(phase, index)
            self._last_reason.pop(slot, None)
            self._last_failure_code = None
        elif verdict.code is not None:
            self._last_reason[slot] = verdict.code
            self._last_failure_code = verdict.code
            if verdict.code == REASON_CLOUD_GEOMETRY_LOCKED:
                self._geometry_rejections[slot] = (
                    self._geometry_rejections.get(slot, 0) + 1
                )
        log_event(
            logger, "correction.crossover_v2_result",
            session_id=self.session_id, phase=phase,
            accepted=verdict.accepted, code=verdict.code or "",
        )
        return verdict.to_relay_dict()

    def _note_accepted(self, phase: str, index: int) -> None:
        if phase not in self._group_indexes:
            self._accepted.add(phase)
            return
        self._group_accepted[phase].add(index)
        if self._group_accepted[phase] >= set(self._group_indexes[phase]):
            self._accepted.add(phase)

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
        self._last_realized_level_match = None
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
        if analysis.candidate is None:
            # Fail FAST, at the capture that produced the unusable analysis.
            # Until the 2026-07-27 timing move this raise happened one call
            # deeper and one line later (``_build_candidate``'s own identical
            # check, still there as the residual) — same exception, same
            # message, same phase, so the host's ``internal_error`` mapping is
            # unchanged. Hoisting it is what keeps that behaviour at MEASURE:
            # the candidate build now happens eight captures later, and a
            # household must not walk the whole cloud for a session that was
            # already unable to produce a candidate at sweep two.
            raise CrossoverV2FlowError("MEASURE analysis produced no candidate")
        self._measure_gate_window_ms = self._measure_gate(analysis)
        # **The fit runs at the last capture before the apply.** Which capture
        # that is depends on the session's own phase list, and there are
        # exactly two shapes:
        #
        # * a session that runs a CLOUD_MEASURE group (every production
        #   measurement session since PR-3b — ``prepare_v2_session`` passes
        #   ``build_v2_cloud_index_phase_map()``) defers the fit, the candidate
        #   build, and the auto-apply trigger to that group's close, so the fit
        #   consumes the cloud's honesty verdict instead of preceding it by
        #   eight captures. Owner decision, 2026-07-27 — the work order's own
        #   pre-registered phase order. See ``_close_measure_cloud_candidate``.
        # * a session with no such group (the pre-cloud 3-entry shape this
        #   class still defaults to) has nothing to wait for, so it builds here
        #   and behaves as it did before the move: same accept, same payload
        #   keys, same auto-apply timing. Scoped precisely, because the
        #   unqualified phrase would be wrong — the candidate it publishes
        #   DOES gain an always-empty ``exclusion_evidence`` key, which is
        #   omitted from ``_core()`` when empty and so leaves the fingerprint
        #   byte-identical. The FLOW is unchanged; the artifact gains one
        #   empty, non-fingerprinted field.
        #
        # The 2026-07-20 "no human Apply gate" ruling is untouched by either
        # branch: apply is still automatic and still needs no tap; only its
        # trigger point moves, and only for a session that has a cloud.
        #
        # On the deferring branch ONLY, the analysis is retained rather than
        # consumed — it is the fit's input and must outlive the prompted cloud
        # walk. The non-deferring branch consumes it in this same call and
        # never stores it: keeping a tens-of-megabytes reference that nothing
        # will ever read is not free on a 1 GB Pi (see the field's own comment
        # in ``__init__`` for the measurement). Exactly one is ever held; a
        # MEASURE re-arm overwrites it, and the group close releases it.
        if PHASE_CLOUD_MEASURE in self._phases:
            self._measure_analysis = analysis
            return PhaseVerdict(True, payload={"measurement_phase": PHASE_MEASURE})
        # The pre-cloud 3-entry shape, which NO production caller constructs
        # any more (``prepare_v2_session`` always builds a cloud map,
        # ``prepare_v2_verify`` maps VERIFY alone). It keeps folding the
        # candidate payload into this verdict, but note that since
        # flow-simplification §2.6 moved the trigger onto the confirm seam,
        # the host no longer reads ``auto_apply`` off a capture verdict — so a
        # future caller reviving this shape has to wire its own apply trigger
        # rather than inherit one. Kept honest here rather than discovered
        # later by a session that measures and never applies.
        return PhaseVerdict(
            True,
            payload={
                "measurement_phase": PHASE_MEASURE,
                **self._publish_measure_candidate(analysis, None),
            },
        )

    def _consume_cloud_position(
        self,
        phase: str,
        index: int,
        attempt: int,
        analysis: ProgramAnalysis,
        result: Any,
    ) -> PhaseVerdict:
        verdict = self._cloud_position_verdict(
            phase, index, attempt, analysis, result
        )
        self._safe_log_diag(
            lambda a, v: self._log_cloud_diag(phase, index, a, v), analysis, verdict
        )
        return verdict

    def _cloud_position_verdict(
        self,
        phase: str,
        index: int,
        attempt: int,
        analysis: ProgramAnalysis,
        result: Any,
    ) -> PhaseVerdict:
        """One prompted position: light per-capture QC, then the group check.

        **Per-position work is deliberately light** (the PR-3b design
        contract): the same locate/linearity screens every phase runs, plus
        "did this capture yield a usable summed response". The group analyses —
        combine, null identification, spec evaluation — run ONCE per group, not
        once per position, so their cost is paid once instead of N times.

        Measured 2026-07-27 on the S0 ten-position corpus (this laptop; a Pi 5
        is slower — :func:`combine_cloud_positions` states the 3-6 s
        across-hosts regime): the **combine is 2.7-2.8 s and dominates
        completely**, while everything layered on it — the null gate, the spec
        evaluation, the carve-out assembly — totals **0.02-0.04 s**. Running
        the set per position would multiply that by N instead of paying it
        once. (This paragraph previously said "~40 s analyses" and "five
        minutes" for an 8-position cloud; no measurement supports either, and
        the real argument does not need them — 2.7 s x 9 is still worth not
        spending.)

        Two VERIFY gates are deliberately NOT applied here, because both assume
        a stationary mic replaying the identical program:

        * gate-comparability (a shorter gate than MEASURE's ⇒ inconclusive) —
          a cloud position's gate legitimately differs from the anchor's, since
          the nearest boundary changes when the mic moves. That is the
          measurement, not a defect.
        * the G3 pilot-transfer step — the reference it compares against is
          "the same chain measuring the same thing"; moving the mic changes
          the acoustic transfer by design, so a step here carries no
          information about the recording chain drifting.
        """
        if not _stimulus_locate_ok(analysis):
            return PhaseVerdict(False, REASON_LOCATE_FAILED)
        if analysis.linearity_ok is False:
            return PhaseVerdict(False, REASON_AGC_BEHAVIORAL_FAIL)
        response = analysis.summed_response
        if response is None:
            # The stimulus located but no summed response came back — the
            # capture carries no curve to combine, so it is not evidence.
            return PhaseVerdict(False, REASON_LOCATE_FAILED)
        prompt = self._prompt_shown_for(phase, index)
        position = _CloudPosition(
            position_id=f"{phase}_{index:02d}",
            index=index,
            attempt=attempt,
            prompt=prompt.text,
            wide=prompt.wide,
            captured_at=time.time(),
            response=response,
            sample_rate_hz=self._verify_program.sample_rate_hz,
            echo_band_hz=self._cloud_echo_band.band_hz,
            signal_band_hz=self._cloud_signal_band_hz,
        )
        self._retain_cloud_position(phase, position, analysis, result)
        if index != self._group_indexes[phase][-1]:
            return PhaseVerdict(True, payload={"position_id": position.position_id})
        return self._close_cloud_group(phase, position)

    def _retain_cloud_position(
        self,
        phase: str,
        position: _CloudPosition,
        analysis: ProgramAnalysis,
        result: Any,
    ) -> None:
        """Record one position in the group and hand it to the evidence seam.

        Idempotent per index: a retaken position REPLACES the earlier take, so
        a group can never carry two curves for one prompted spot.
        """
        retained = self._group_positions[phase]
        retained[:] = [p for p in retained if p.index != position.index]
        retained.append(position)
        retained.sort(key=lambda p: p.index)
        if self._seams.retain_position is None:
            return
        gating = getattr(position.response, "gating", None) or {}
        metadata = {
            "position_id": position.position_id,
            "phase": phase,
            "index": position.index,
            "attempt": position.attempt,
            "prompt": position.prompt,
            "wide": position.wide,
            "captured_at": position.captured_at,
            "session_id": self.session_id,
            "gate_window_ms": _gate_window_ms(position.response),
            "validity_floor_hz": getattr(
                position.response, "validity_floor_hz", None
            ),
            "gating_applied": bool(gating.get("applied")),
            "summed_ripple_db": analysis.summed_ripple_db,
            "glitch_detected": bool(analysis.glitch_detected),
        }
        try:
            self._seams.retain_position(position.position_id, result, metadata)
        except (OSError, RuntimeError, TypeError, ValueError):
            # Evidence retention is forensics, never a gate: a full disk must
            # not turn an acoustically-good position into a retake.
            log_event(
                logger, "correction.crossover_v2_position_retain_failed",
                level=logging.WARNING,
                session_id=self.session_id, phase=phase,
                position_id=position.position_id, exc_info=True,
            )

    def _close_cloud_group(
        self, phase: str, position: _CloudPosition
    ) -> PhaseVerdict:
        """The group-end combine, and the one bounded retake it can ask for.

        Combines the group's retained positions exactly ONCE (S3 review
        finding, 2026-07-26: an earlier revision called
        ``combine_cloud_positions`` a second time from the pipeline step
        below — measured seconds-per-combine, 3-6 s across runs/hosts on the
        S0 ten-position corpus, worse on a Pi 5 (N2 review finding,
        2026-07-27: restated from an earlier "5.6-6.2 s" point figure that
        did not reproduce across hosts). With ``GEOMETRY_RETRY_POSITIONS = 2``
        allowing up to 3 close attempts per group, the pre-fix worst case was
        3 × 2 = 6 combines, not the earlier "4x" claim — real operator
        seconds this wiring does not need to spend). Both the retry-gating
        verdict AND the honest-instrument pipeline read the SAME ``combined``
        object.
        """
        positions = self._group_positions[phase]
        combined = combine_cloud_positions(positions)
        verdict = _geometry_verdict_from_combined(combined, len(positions))
        retries = self._geometry_retries_used[phase]
        retry_warranted = (
            verdict.get("locked") is True
            # ``thin_evidence`` marks a verdict resting on the bare minimum
            # number of usable echo estimates (see GeometryLock's docstring —
            # it is a cliff, not a gradient). Asking an operator to walk two
            # more positions on that basis spends real session minutes on a
            # verdict the instrument itself qualifies, so a thin lock is
            # disclosed and accepted rather than retried.
            and verdict.get("thin_evidence") is not True
            and retries < GEOMETRY_RETRY_POSITIONS
            # …and never AFTER the group has already recorded a verdict. A
            # VOLUNTARY retake (§2.6) re-enters this close with the group
            # already closed, and the retry branch below DROPS the take at
            # this index — which, on a voluntary retake, is the only copy
            # (``_retain_cloud_position`` replaced the original in place).
            # Dropping it would leave the household with LESS evidence than
            # before they chose to redo a spot, which is the one thing the
            # retake contract promises can never happen. So: re-combine, keep
            # the verdict honest with the new take, and accept.
            and phase not in self._group_geometry
        )
        if retry_warranted:
            self._geometry_retries_used[phase] = retries + 1
            # Drop the take being replaced FROM THE CLOUD. This is what the
            # protocol's retake lever means — the same index is measured again
            # — not a claim that dropping beats appending (see
            # GEOMETRY_RETRY_POSITIONS, where that claim was withdrawn). Its
            # evidence artifact stays on disk under its own attempt-qualified
            # path: the capture was fine, and a forensic record of what the
            # operator actually walked is worth more than a tidy bundle.
            retained = self._group_positions[phase]
            retained[:] = [p for p in retained if p.index != position.index]
            log_event(
                logger, "correction.crossover_v2_cloud_geometry_retry",
                session_id=self.session_id, phase=phase,
                retry=retries + 1, of=GEOMETRY_RETRY_POSITIONS,
                median_tau_us=verdict.get("median_tau_us"),
                clustered_fraction=verdict.get("clustered_fraction"),
            )
            prompt = CLOUD_GEOMETRY_RETRY_PROMPTS[
                min(retries, len(CLOUD_GEOMETRY_RETRY_PROMPTS) - 1)
            ]
            return PhaseVerdict(
                False, REASON_CLOUD_GEOMETRY_LOCKED,
                payload={"prompt": prompt, "geometry": dict(verdict)},
            )
        self._group_geometry[phase] = verdict
        log_event(
            logger, "correction.crossover_v2_cloud_group_complete",
            session_id=self.session_id, phase=phase,
            positions=len(self._group_positions[phase]),
            geometry_locked=bool(verdict.get("locked")),
            geometry_reason=verdict.get("reason") or "",
            thin_evidence=bool(verdict.get("thin_evidence")),
            geometry_retries=retries,
        )
        # S4 review finding (2026-07-26): the group's accept is decided above
        # (the log line just fired) — the honesty pipeline below is
        # diagnostic/disclosure machinery layered on TOP of that decision, and
        # must never be able to cost the group its accept.
        #
        # **Scope, corrected 2026-07-27 (N1):** "decided" is not "recorded".
        # ``_note_accepted`` runs in ``consume_capture`` AFTER this method
        # returns, so a raise anywhere below — including the candidate build,
        # which is deliberately NOT wrapped — unwinds before the phase is
        # marked accepted. The resulting state is honest but worth naming:
        # ``event=correction.crossover_v2_cloud_group_complete`` is in the
        # journal, the group's geometry verdict is on the conductor, and the
        # phase is NOT in ``accepted_phases`` — so nothing durable claims a
        # completed group, and the host maps the raise to a terminal
        # ``internal_error`` screen. The claim this wrap makes is therefore
        # about the PIPELINE only: a named-family pipeline exception cannot
        # cost the accept. It says nothing about the candidate build below,
        # which is allowed to fail the capture, because it is the session's
        # product rather than its disclosure.
        # assemble_cloud_group_result's own try/except (ValueError, TypeError,
        # IndexError, AttributeError -- the documented raise surface of
        # everything it calls) and _run_cloud_pipeline's own try/except around
        # the publish_cloud seam (OSError, RuntimeError, TypeError, ValueError
        # -- the same family every other evidence-publish boundary in this
        # file uses) each guard their own step; this wrap is the outer
        # backstop for that SAME six-member named family (N1 review finding,
        # 2026-07-27: the prior wording claimed this was unconditional --
        # "structurally true rather than merely usually true" -- which
        # overclaimed past what the code does. A KeyError, or anything else
        # outside these six names, is NOT caught here either and propagates
        # uncaught exactly as assemble_cloud_group_result's own docstring
        # discloses -- pinned by
        # test_an_unnamed_exception_family_still_propagates_through_the_outer_wrap).
        # Scoped claim: a NAMED-family exception cannot cost the accept; the
        # residual propagates by design.
        try:
            self._run_cloud_pipeline(phase, combined, positions)
        except (OSError, RuntimeError, TypeError, ValueError, IndexError, AttributeError):
            log_event(
                logger, "correction.crossover_v2_cloud_pipeline_call_failed",
                level=logging.WARNING,
                session_id=self.session_id, phase=phase, exc_info=True,
            )
        payload: dict[str, Any] = {
            "position_id": position.position_id,
            "group_complete": phase,
            "geometry": dict(verdict),
        }
        if phase == PHASE_CLOUD_MEASURE:
            # The pre-apply cloud's geometry verdict and disclosure pipeline are
            # in hand — but the FIT no longer runs here. Flow-simplification
            # §2.6: firing fit + auto-apply on this acceptance made the final
            # prompted position the one spot in the whole session a household
            # could not choose to redo, because the speaker was already being
            # retuned by the time the "Retake" control could have been tapped.
            # The fit moves to :meth:`confirm_cloud_measure_group`, which the
            # host calls when the household confirms PAST the final position.
            # No trust gate moved: the fit still runs only after the full
            # cloud, under the same gates — one user tap now sits in front of
            # it. Stash the combine so the confirm does not pay for a second
            # one (measured 2.7-6 s, see this method's own docstring).
            self._group_combined[phase] = combined
            payload["awaiting_confirm"] = True
        return PhaseVerdict(True, payload=payload)

    def cloud_measure_group_awaiting_confirm(self) -> bool:
        """Whether the pre-apply cloud is walked but not yet confirmed.

        True exactly between the final prompted position's acceptance and the
        household's confirmation past it — the window in which a voluntary
        retake of that position is still meaningful (§2.6).
        """
        return (
            PHASE_CLOUD_MEASURE in self._group_combined
            and self._candidate is None
        )

    def confirm_cloud_measure_group(self, index: int) -> dict[str, Any] | None:
        """Close out the pre-apply cloud once the household confirms past it.

        **This is the group-close seam** (§2.6). ``index`` is the 1-based wire
        index of the begin that carries the confirmation — in practice VERIFY's,
        posted when the household taps through the "all spots done" screen.
        Returns the same ``{candidate, auto_apply}`` payload
        :meth:`_close_cloud_group` used to fold into the final position's
        verdict, so the host fires the identical auto-apply it always did;
        returns ``None`` when there is nothing to confirm.

        Why a separate method the HOST calls, rather than folding it into
        :meth:`authorize_begin`: admission stays bookkeeping — budget, defer,
        refuse — and the one call that fits a correction and hands it to the
        apply transaction stays visible at the host boundary, next to the
        ``persist_conductor_state`` that must precede the apply thread.

        Fires at most once per session: the guard is ``self._candidate``, which
        :meth:`_publish_measure_candidate` sets. A raise leaves it unset, so a
        genuinely retryable failure can be retried; a session with no cloud
        group (the 3-entry shape, the verify-only re-arm) never has anything
        stashed and always returns ``None``.
        """
        if not self.cloud_measure_group_awaiting_confirm():
            return None
        last_index = self._group_indexes[PHASE_CLOUD_MEASURE][-1]
        if int(index) <= last_index:
            # A begin still INSIDE the group (a retake of the final position)
            # is not a confirmation — it is the household using the window
            # this seam exists to keep open.
            return None
        log_event(
            logger, "correction.crossover_v2_cloud_group_confirmed",
            session_id=self.session_id, phase=PHASE_CLOUD_MEASURE,
            positions=len(self._group_positions[PHASE_CLOUD_MEASURE]),
            confirmed_at_index=int(index),
        )
        return self._close_measure_cloud_candidate(
            self._group_combined[PHASE_CLOUD_MEASURE]
        )

    def _close_measure_cloud_candidate(self, combined: Any) -> dict[str, Any]:
        """Fit, build, publish, and hand the candidate to auto-apply.

        The relocated tail of :meth:`_measure_verdict` (owner decision,
        2026-07-27). It runs once per session, driven by
        :meth:`confirm_cloud_measure_group` (flow-simplification §2.6 moved
        the trigger from the final position's ACCEPTANCE to the household's
        confirmation past it), and returns the two payload keys the host's
        auto-apply wiring reads — which keys on ``auto_apply`` and never on a
        phase.

        **The fit now consumes the cloud** (plan interpretation call (A), the
        wiring half of PR-6): :func:`_cloud_fit_evidence` turns this group's
        closed pipeline result into the merged honesty intervals and the
        cross-position spread that :func:`compose_envelope`'s
        ``spatial_exclusion_limit`` / ``position_stability_limit`` terms
        consume. A group whose pipeline did not become available yields
        ``None`` and the fit runs exactly as it did before this move —
        disclosed, not silent (see :func:`_cloud_fit_evidence`).

        Reaching this with ``_measure_analysis`` already ``None`` means MEASURE
        was accepted by a DIFFERENT conductor instance — the same-session
        ``hydrate`` branch, which carries ``accepted_phases`` but no analysis.
        (The tail of this method releases the analysis, but that release
        happens strictly AFTER this check and only once per group, so it can
        never be what this branch is seeing — see the release comment for why
        a second close of one group is structurally impossible.)
        **Production cannot reach it**: ``prepare_v2_session`` hydrates against
        a freshly MINTED relay session id, so the id never matches and hydrate
        always takes the fresh-start-at-CHECK branch (§5.6's own rule). If it
        is ever reached, this raises rather than returning a payload without
        ``auto_apply``: a session with no candidate can never release VERIFY's
        ``on_apply`` hold, so the alternative is a silent stall until the relay
        times out, and an honest ``internal_error`` screen beats that.
        """
        if self._measure_analysis is None:
            raise CrossoverV2FlowError(
                "cloud-measure group closed with no retained MEASURE analysis"
            )
        payload = self._publish_measure_candidate(
            self._measure_analysis, self._cloud_fit_evidence(combined)
        )
        # Released on success: the fit has consumed it and nothing reads it
        # again, so a tens-of-megabytes reference should not survive to the end
        # of a session that still has six captures to go (see the field's
        # comment in ``__init__``).
        #
        # **Why releasing cannot strand a re-delivered capture.** Releasing
        # makes a SECOND call raise instead of rebuilding, so it is only safe
        # if a second call cannot happen — and it cannot: the sole caller
        # (``confirm_cloud_measure_group``) refuses once ``self._candidate`` is
        # set, which the line above does. Neither retake shape is a
        # counter-example: a GEOMETRY retake returns REJECTED from
        # ``_close_cloud_group`` well before any confirm, and a VOLUNTARY
        # retake (§2.6) is only admitted while the confirm has not happened,
        # so it re-closes the group and re-stashes the combine without ever
        # reaching here twice. Left in place on a raise — that session is
        # already failing, and the conductor is about to be discarded.
        self._measure_analysis = None
        return payload

    def _publish_measure_candidate(
        self, analysis: ProgramAnalysis, cloud: "_CloudFitEvidence | None",
    ) -> dict[str, Any]:
        """Build, publish, and hand one candidate to auto-apply.

        The single build/publish path, called from whichever capture is the
        last before the apply for this session's shape — the CLOUD_MEASURE
        group close when the session runs one, MEASURE's own accept when it
        does not (see :meth:`_measure_verdict`). Returns the two payload keys
        the host's ``consume()`` seam reads; that seam keys on ``accepted``
        plus ``auto_apply`` and never on a phase, which is why the timing move
        needed no host change.

        **The accountability seam (linearization-integrity PR-L4).** This is the
        last moment before the speaker is touched, and it is where the two
        load-bearing assertions live — the realized inter-driver level (item 1)
        and the spec-graded prediction (item 2). Both run AFTER the build and
        BEFORE ``self._candidate`` is set and ``publish_candidate`` fires, so a
        refusal leaves no candidate for anything downstream to apply, and the
        confirm seam's ``CaptureBeginRefused`` arm persists a named reason with
        its own household copy.

        They live here and not inside :meth:`_build_candidate` on purpose: that
        method's SF2 arm catches a fit-engine failure and degrades to the
        trims-only path, which is the right answer for a BUG in the fit and
        exactly the wrong answer for an accountability refusal — quietly
        shipping an unlinearized candidate is the silent-failure shape this PR
        exists to remove.

        On the pre-cloud 3-entry shape — which no production caller constructs
        (see :meth:`_measure_verdict`'s own note) — this method is reached from
        ``consume_capture`` instead, so a refusal propagates out of THAT seam
        rather than the confirm one and lands in the host's catch-all as
        ``internal_error``. Still loud, still leaves the speaker untouched, just
        without the named screen; a caller reviving that shape has to wire its
        own refusal handling, exactly as it has to wire its own apply trigger.
        """
        candidate = self._build_candidate(analysis, cloud)
        # VERIFY-prediction coherence fix (hardware-validation-caught, #1668
        # PR-D): when this attempt fitted Layer-1a linearization (fitted OR
        # trim_rejected — both emit the correction filters, see
        # ``_fit_linearization``'s tail), the persisted prediction VERIFY
        # compares against must be the LINEARIZED model, the exact thing the
        # emitted graph now carries — never the raw-branch one. The
        # ineligible/fit_failed path is untouched: ``_last_linearized_
        # predicted_sum`` stays ``None`` there, so this stays byte-identical
        # to ``analysis.predicted_sum``, exactly as before this fix. It is
        # computed here rather than at MEASURE because the fit is here; nothing
        # reads it in between (``_cloud_priors`` deliberately carries no
        # ``predicted_sum``, and VERIFY is the next capture after this close).
        predicted_sum = (
            self._last_linearized_predicted_sum
            if self._last_linearized_predicted_sum is not None
            else analysis.predicted_sum
        )
        # PR-L4: the last gate before the speaker is touched. Raises
        # CaptureBeginRefused, so nothing below runs — no candidate is stashed,
        # none is published, and the payload that triggers auto-apply is never
        # returned.
        self._assert_accountable(predicted_sum, analysis.predicted_sum)
        self._candidate = candidate
        self._measure_predicted_sum = predicted_sum
        self._seams.publish_candidate(candidate)
        log_event(
            logger, "correction.crossover_v2_candidate_built",
            session_id=self.session_id,
            candidate_fingerprint=candidate.fingerprint,
            # Which linearization path this candidate's build took. This field
            # lived on ``correction.crossover_v2_measure_diag`` until the
            # timing move; it could not stay there, because that line is
            # emitted eight captures before the fit now runs and would report
            # "" forever (the retired-field treatment PR-5 gave the per-capture
            # ``flatness_*`` fields, for the same reason).
            linearization=self._last_linearization_outcome,
            # Did the cloud's honesty verdict actually reach the envelope?
            cloud_evidence=cloud is not None,
            excluded_bands=len(cloud.excluded_bands_hz) if cloud else 0,
            cloud_positions=cloud.n_positions if cloud else 0,
        )
        return {
            "candidate_fingerprint": candidate.fingerprint,
            # Tells the host to trigger auto-apply immediately (§owner ruling,
            # 2026-07-20 — automatic, never a human tap). Every candidate that
            # reaches this point cleared MEASURE's trust gates already (at its
            # own capture, however many captures back this session's shape puts
            # it), so this is unconditionally True here, not a second decision.
            "auto_apply": True,
        }

    def _refuse(self, code: str) -> "CaptureBeginRefused":
        """Build the refusal for ``code``, with the registry's own copy, and
        record it as this conductor's failure code.

        One construction point so a refusal can never ship a bare code where a
        household expects a sentence (:data:`REASON_REGISTRY` is the §5.10 SSOT
        for both).

        **Stamping ``_last_failure_code`` is the load-bearing half**, not
        bookkeeping. The host's ``CaptureBeginRefused`` arm persists
        ``conductor.last_failure_code`` and falls back to
        :data:`REASON_RELAY_TIMEOUT` when it is unset — so a refusal that
        raised without stamping would reach the household as "The measurement
        link timed out", a false statement about a session that was refused on
        purpose. Raising through this one constructor is what makes the
        registry copy above actually the copy that renders.
        """
        spec = REASON_REGISTRY[code]
        self._last_failure_code = code
        return CaptureBeginRefused(code, spec.message or spec.banner)

    def _assert_accountable(
        self, predicted_sum: Any, raw_predicted_sum: Any = None,
    ) -> None:
        """The two load-bearing PR-L4 assertions, run before the apply fires.

        Raises :class:`CaptureBeginRefused` with a named
        :data:`REASON_REGISTRY` code — the host's own refusal arm then persists
        it and the envelope renders its copy, so a refusal here reaches the
        household as a sentence rather than a stall. Returns ``None`` when both
        assertions hold; the caller proceeds to publish.

        Order matters: item 1 runs first because it is the *specific* diagnosis
        (the two drivers will not end up at matching levels) and item 2 is the
        *general* one (this correction does not measure better). When both are
        true, naming the specific cause is more useful to whoever reads the
        journal, and the household copy is more actionable.
        """
        # --- item 1: the inter-driver realized level ---------------------
        match = self._last_realized_level_match
        if match is not None and not match.matched:
            log_event(
                logger, "correction.crossover_v2_level_match_refused",
                level=logging.ERROR, session_id=self.session_id,
                reason=REASON_DRIVER_LEVELS_DISAGREE,
                difference_db=round(float(match.difference_db), 3),
                tolerance_db=match.tolerance_db,
                level_w_db=round(float(match.level_w_db), 3),
                level_t_db=round(float(match.level_t_db), 3),
            )
            raise self._refuse(REASON_DRIVER_LEVELS_DISAGREE)

        # --- item 2: spec-grade the prediction ---------------------------
        #
        # PR-6b made auto-apply unconditional at this seam ("this is
        # unconditionally True here, not a second decision"). This deliberately
        # AMENDS that, under the linearization-integrity work order's PR-L4
        # item 2 (docs/linearization-integrity-plan.md), which is the sanction
        # for the change: on 2026-07-27 the honest flatness instrument failed
        # all three bands two seconds before an unconditional auto-apply, and
        # its verdict reached zero surfaces. PR-6b's claim — that MEASURE's
        # trust gates already decided — was true about the CAPTURE and silent
        # about the CORRECTION. This adds the missing half: the capture is
        # trusted, and now the thing built from it has to show its work.
        #
        # **BEFORE and AFTER, on the same instrument** (PR-L4 review B1). The
        # first cut of this gate compared the model's residual against the
        # MEASURED pre-apply cloud's, which is not a comparison: an
        # eight-position in-room spatial mean and a gated two-branch model at
        # the mark are different instruments in different frames, so the margin
        # between them is room-sized. Held to a constant, excellent correction
        # (predicted pooled 0.858 dB) the reviewer varied only the ROOM and
        # watched the verdict flip — the shipped fixture applied at +0.333, and
        # every BETTER room refused. That is exactly backwards: it punished
        # good rooms, and it tightened as the correction improved. Worse, it was
        # a live trap — an owner who undoes first re-measures a decent speaker
        # and re-runs into a stricter bar.
        #
        # The fix is to ask the question the gate was always trying to ask, of
        # one instrument: grade the RAW pre-fit two-branch prediction and the
        # LINEARIZED one through the IDENTICAL `spec_report_for_predicted_sum`,
        # and require the correction to move ITS OWN model materially. Same
        # branches, same grid, same evaluator, same position — the room cancels
        # because it is not in either term.
        if raw_predicted_sum is None or self._last_linearized_predicted_sum is None:
            # No fit ran this attempt (ineligible mic tier, or the fit failed
            # into SF2's trims-only fallback), so `predicted_sum` IS
            # `raw_predicted_sum` — the same object. Grading a thing against
            # itself always returns "no improvement", which would refuse every
            # trims-only candidate on the strength of arithmetic rather than
            # evidence. Abstain, loudly.
            self._log_prediction_ledger(reason="no_linearization")
            return
        after = spec_report_for_predicted_sum(predicted_sum)
        if after is None:
            self._log_prediction_ledger(reason="prediction_ungradeable")
            return
        if after.overall_passed:
            # A prediction that meets the spec on its own needs no improvement
            # argument, and gating an in-spec result on "how much did it
            # improve" would refuse the flattest speakers hardest.
            self._log_prediction_ledger(reason="predicted_in_spec", after=after)
            return
        before = spec_report_for_predicted_sum(raw_predicted_sum)
        if before is None:
            self._log_prediction_ledger(reason="baseline_ungradeable", after=after)
            return
        from jasper.active_speaker.flat_spec import spec_convergence_residual

        after_rms_db = spec_convergence_residual(after).rms_db
        before_rms_db = spec_convergence_residual(before).rms_db
        if after_rms_db is None or before_rms_db is None:
            self._log_prediction_ledger(
                reason="residual_unevaluable", after=after, before=before,
            )
            return
        improvement_db = float(before_rms_db) - float(after_rms_db)
        if improvement_db >= PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB:
            self._log_prediction_ledger(
                reason="improved", after=after, before=before,
                improvement_db=improvement_db,
            )
            return
        self._log_prediction_ledger(
            reason=REASON_CORRECTION_NOT_AN_IMPROVEMENT, after=after, before=before,
            improvement_db=improvement_db, level=logging.ERROR,
        )
        raise self._refuse(REASON_CORRECTION_NOT_AN_IMPROVEMENT)

    def _log_prediction_ledger(
        self,
        *,
        reason: str,
        after: Any = None,
        before: Any = None,
        improvement_db: float | None = None,
        level: int = logging.INFO,
    ) -> None:
        """One ledger line per session for item 2's gate, on EVERY path.

        Mirrors item 1's ``correction.crossover_v2_realized_level_match``, which
        logs whether or not it refuses (PR-L4 review S4). A gate that only
        speaks when it fires leaves "it passed" and "it never ran" looking
        identical in the journal — the exact ambiguity this PR exists to remove,
        and the one a field diagnosis of a dark speaker would need first.
        """
        from jasper.active_speaker.flat_spec import spec_convergence_residual

        def _rms(report: Any) -> float | None:
            if report is None:
                return None
            value = spec_convergence_residual(report).rms_db
            return round(float(value), 3) if value is not None else None

        log_event(
            logger, "correction.crossover_v2_prediction_gate",
            level=level, session_id=self.session_id, reason=reason,
            before_rms_db=_rms(before),
            after_rms_db=_rms(after),
            after_passed=(after.overall_passed if after is not None else None),
            improvement_db=(
                round(float(improvement_db), 3) if improvement_db is not None else None
            ),
            required_db=PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB,
        )

    def _cloud_fit_evidence(self, combined: Any) -> "_CloudFitEvidence | None":
        """This group's honesty verdict, in the shape the fit envelope takes.

        ``None`` — the fit runs with no cloud terms, byte-identical to every
        candidate built before the timing move — in exactly two cases, and both
        are disclosed rather than silent:

        * the positions could not be combined at all (``combined is None``);
        * the combine succeeded but the honesty pipeline did not become
          available (a null-gate or spec-evaluator failure, already logged as
          ``correction.crossover_v2_cloud_pipeline_failed``).

        The second is **all-or-nothing on purpose.** A failed pipeline still
        leaves ``combined.excluded_bands_hz`` — the power-vs-median screen's
        own intervals — and it would be easy to hand the fit those. That is
        precisely the mask-alone read issue #1742 item 4 forbids: the screen
        structurally cannot see a position-invariant null (plan "S0 executed"
        § e.1 — 0 of 5462 bins in 8-16 kHz on the S0 corpus), so a
        screen-only mask would exclude the interference the cloud CAN see while
        silently correcting the interference it cannot, which is worse than
        excluding nothing and being honest about it. One verdict, or none.
        """
        if combined is None:
            return None
        result = self._group_cloud_result.get(PHASE_CLOUD_MEASURE) or {}
        if result.get("available") is not True:
            log_event(
                logger, "correction.crossover_v2_fit_without_cloud",
                level=logging.WARNING, session_id=self.session_id,
                reason=str(result.get("reason") or "no_pipeline_result"),
            )
            return None
        intervals = tuple(
            (float(band[0]), float(band[1]))
            for band in result.get("merged_excluded_bands_hz") or ()
        )
        return _CloudFitEvidence(
            excluded_bands_hz=intervals,
            band_spread=tuple(combined.band_spread),
            n_positions=int(combined.n_positions),
        )

    def _run_cloud_pipeline(
        self, phase: str, combined: Any, positions: Sequence[_CloudPosition],
    ) -> None:
        """PR-4: the honest-instrument pipeline, run once per CLOSED group.

        ``combined`` is the SAME object ``_close_cloud_group`` just derived
        its retry-gating verdict from — ONE combine per group close (S3
        review finding, 2026-07-26), never a second call to
        :func:`combine_cloud_positions`. ``positions`` is that same group's
        retained list, read for exactly one thing: its gated validity floor
        (:func:`cloud_validity_floor_hz`), which clamps the spec band's lower
        edge (plan PR-5).

        Never raises and never affects the accepted verdict already decided
        above — this is diagnostic/disclosure machinery, not a capture gate.
        """
        result = assemble_cloud_group_result(
            combined,
            echo_band_hz=self._cloud_echo_band.band_hz,
            echo_band_provenance=self._cloud_echo_band.disclosure(),
            validity_floor_hz=cloud_validity_floor_hz(positions),
            tier=self._tier,
        )
        self._group_cloud_result[phase] = result
        # PR-5: the spec verdict a session's journal carries. It replaces the
        # per-VERIFY-capture ``flatness_*`` fields ``_log_verify_diag`` used
        # to log from the retired capture-grid construction — same operator
        # question, answered by the instrument that can actually answer it,
        # logged once per group instead of once per capture.
        flatness = result.get("flatness") if result.get("available") else None
        flatness = flatness if isinstance(flatness, Mapping) else {}
        log_event(
            logger, "correction.crossover_v2_cloud_spec",
            session_id=self.session_id, phase=phase,
            available=bool(result.get("available")),
            reason=str(result.get("reason") or ""),
            spec_passed=flatness.get("passed"),
            spec_evaluable=flatness.get("evaluable"),
            flatness_max_db=flatness.get("max_db"),
            flatness_max_hz=flatness.get("max_hz"),
            flatness_rms_db=flatness.get("rms_db"),
            spec_n_excluded=flatness.get("n_excluded"),
            validity_floor_hz=result.get("validity_floor_hz"),
        )
        if self._seams.publish_cloud is not None:
            try:
                self._seams.publish_cloud(
                    phase, self._group_cloud_result[phase]
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                # Mirrors _retain_cloud_position's fail-soft boundary: evidence
                # publication is forensics, never a gate, so a full disk or a
                # write-once conflict must not undo the group's own accept.
                log_event(
                    logger, "correction.crossover_v2_cloud_publish_failed",
                    level=logging.WARNING,
                    session_id=self.session_id, phase=phase, exc_info=True,
                )

    def _log_cloud_diag(
        self,
        phase: str,
        index: int,
        analysis: ProgramAnalysis,
        verdict: PhaseVerdict,
    ) -> None:
        response = analysis.summed_response
        log_event(
            logger, "correction.crossover_v2_cloud_diag",
            session_id=self.session_id, phase=phase, index=index,
            accepted=verdict.accepted, code=verdict.code or "",
            positions_in=len(self._group_positions.get(phase, ())),
            gate_window_ms=_gate_window_ms(response),
            validity_floor_hz=getattr(response, "validity_floor_hz", None),
            summed_ripple_db=analysis.summed_ripple_db,
            linearity_ok=analysis.linearity_ok,
            glitch=analysis.glitch_detected,
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
                payload={"tracking": dict(tracking)},
            )
        self._verify_outcome = "pass"
        return PhaseVerdict(
            True, payload={
                "measurement_phase": PHASE_VERIFY,
                "tracking": dict(tracking),
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
            # (A ``linearization`` field lived here until the 2026-07-27
            # timing move. It reported which path the candidate build took,
            # and the candidate build now happens eight captures later, at the
            # cloud-measure group close — so this line could only ever have
            # reported "". It moved to ``correction.crossover_v2_candidate_built``
            # rather than being kept as a permanently-empty field, the same
            # treatment PR-5 gave the per-capture ``flatness_*`` fields when
            # their subject moved to the cloud.)
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
        # (The ``flatness_*`` fields this line carried until PR-5 came from
        # the retired per-capture construction. The spec verdict is logged
        # once per closed group instead — ``correction.crossover_v2_cloud_spec``
        # in ``_run_cloud_pipeline``.)
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

    def _build_candidate(
        self, analysis: ProgramAnalysis, cloud: _CloudFitEvidence | None = None,
    ) -> Any:
        from jasper.active_speaker.measured_crossover_candidate import (
            MeasuredCrossoverAlignment,
            MeasuredCrossoverCandidate,
        )

        cand = analysis.candidate
        if cand is None:
            # The residual. ``_measure_verdict`` hoisted this same check to the
            # capture that produces the analysis (2026-07-27 timing move), so
            # reaching it here means a caller that did not walk that path.
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
                    analysis, cand, cloud
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
                # PR-L4 item 1: a fit that raised part-way may already have
                # written its level verdict. Clear it with the rest of the fit's
                # output — a verdict about branches this candidate no longer
                # carries is worse than no verdict, because the assertion at
                # publish time would grade the wrong thing.
                self._last_realized_level_match = None
                self._last_linearization_outcome = "fit_failed"

        return MeasuredCrossoverCandidate(
            program_id=analysis.program_id,
            analysis=_analysis_json(analysis),
            source_preset=self._preset,
            role_attenuations_db=role_attenuations_db,
            alignment=alignment,
            linearization=linearization,
            # The exclusion reason of record (plan PR-6b). Empty — the
            # pre-move shape — whenever no cloud evidence reached the fit,
            # INCLUDING when the fit itself failed above: a record of what the
            # envelope consumed must not ride a candidate whose corrections
            # came from the trims-only fallback instead.
            exclusion_evidence=(
                self._exclusion_evidence_json(cloud)
                if cloud is not None and linearization
                else {}
            ),
            # Gauge fix (2026-07-24): the single writer's own verdict,
            # stamped verbatim onto the candidate at the exact moment it
            # reaches its final value for this attempt — see
            # MeasuredCrossoverCandidate.linearization_outcome's own
            # docstring for why this module never re-derives it.
            linearization_outcome=self._last_linearization_outcome,
        )

    def _exclusion_evidence_json(self, cloud: _CloudFitEvidence) -> dict[str, Any]:
        """The fit's cloud inputs, as the candidate's exclusion reason of record.

        Everything the two cloud envelope terms actually consumed, plus the
        registry that justifies the intervals — enough that a reader holding
        only ``candidate.json`` can re-derive ``spatial_exclusion_limit`` and
        ``position_stability_limit`` and see WHY a band went uncorrected. The
        registry is re-read from this group's own pipeline result and
        serialized by :func:`_null_registry_to_dict`, the one owner of that
        shape, so the candidate's copy and ``cloud_measure.json``'s cannot
        disagree.

        ``band_spread`` is carried as the plain per-band numbers rather than
        the dataclass: this is persisted JSON, and the two fields the term
        reads (``sigma_db`` and the band edges it applies over) are the two a
        reader needs to check it. ``max_sigma_db`` rides along because
        ``position_stability_limit``'s docstring turns on the distinction
        between the two spreads, and a reader auditing the choice needs to see
        the number that was NOT used.
        """
        result = self._group_cloud_result.get(PHASE_CLOUD_MEASURE) or {}
        registry = result.get("null_registry")
        return {
            "phase": PHASE_CLOUD_MEASURE,
            "excluded_bands_hz": [list(band) for band in cloud.excluded_bands_hz],
            "n_positions": cloud.n_positions,
            "band_spread": [
                {
                    "center_hz": float(band.center_hz),
                    "f_lo": float(band.f_lo),
                    "f_hi": float(band.f_hi),
                    "sigma_db": float(band.sigma_db),
                    "max_sigma_db": float(band.max_sigma_db),
                    "n_bins": int(band.n_bins),
                }
                for band in cloud.band_spread
            ],
            "null_registry": dict(registry) if isinstance(registry, Mapping) else {},
        }

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
        self,
        analysis: ProgramAnalysis,
        cand: Any,
        cloud: _CloudFitEvidence | None = None,
    ) -> tuple[dict[str, float], dict[str, Any]]:
        """Fit both drivers, apply the correction in the linear domain, and
        re-solve the trim from the LINEARIZED branch pair — the ordering
        the design doc calls out as structurally defusing #1667's band-
        average trim bias. Returns ``(role_attenuations_db, linearization)``;
        falls back to the ANCHORED trim pair (with a WARNING) when the
        ripple-optimal tweeter re-solve drifts implausibly far from that anchor.
        The anchor is each branch's own raw trim plus the level its emitted
        cascade removed from its reference band
        (``LinearizationFit.correction_giveback_db``) — level-preserving by
        construction, replacing the overlap-band solve seed that under-returned
        the give-back on the 2026-07-24 JTS3 runs.

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
            # The cloud seam (plan PR-6a's two optional terms, wired here by
            # the 2026-07-27 timing move — this is the ONLY production caller
            # that supplies them). All three arguments are ``None`` when no
            # cloud verdict was available, and ``compose_envelope`` documents
            # that case as byte-identical to an envelope composed before the
            # terms existed. They can only ever NARROW allowed depth: they
            # enter the same ``np.min`` as every other term, so no cloud can
            # buy the fit permission it did not already have.
            envelope = compose_envelope(
                role, resp,
                excited_band_hz=excited_band_hz[role],
                mic_tier=mic_tier,
                driver_class=self._driver_class_by_role.get(role, "unknown"),
                sigma_db=sigma_db,
                excluded_bands_hz=cloud.excluded_bands_hz if cloud else None,
                band_spread=cloud.band_spread if cloud else None,
                n_positions=cloud.n_positions if cloud else None,
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

        # Each branch's OWN excited-and-gated validity span, the shape
        # `branch_level_bands_hz` takes (PR-L3). Mirrors
        # `program_analysis._build_candidate`'s own `_span` helper exactly —
        # the declared sweep band, floored by the shared reflection floor —
        # so the realized-level check below reads the same frame the raw trim
        # solve read, one layer up on the linearized branches.
        def _span(role: str) -> tuple[float, float]:
            span_lo, span_hi = excited_band_hz[role]
            if branch_floor_hz is not None and math.isfinite(branch_floor_hz):
                span_lo = max(span_lo, branch_floor_hz)
            return float(span_lo), float(span_hi)

        woofer_span = _span(woofer_role)
        tweeter_span = _span(tweeter_role)

        # ANCHORED give-back (#1668, replaces the overlap-band solve seed after
        # the 2026-07-24 JTS3 runs). Each branch's linearized trim is its own
        # COMMITTED raw trim plus ``LinearizationFit.correction_giveback_db`` —
        # the fit engine's SSOT, the MEASURED before-vs-after level delta of
        # that branch's own reference (core) band. Because the quantity added
        # back IS the measured level change of the band being restored, this
        # restores each branch's audible band to the pre-correction system level
        # the raw candidate already accepted — with no flat-core assumption, no
        # solver prediction, no min() reasoning, and no cross-branch coupling.
        #
        # Why not the old `solve_branch_trims(W_lin, T_lin)` band-average seed:
        # it averaged over the CROSSOVER OVERLAP band (1.43-2.83 kHz on JTS3),
        # which is the wrong reference for a top-octave correction on two
        # counts — the tweeter's LR4 high-pass skirt lives there, and a
        # power-domain mean weights the loudest (least-cut) bins hardest, so the
        # average is dragged toward the region the shelf barely touches; and the
        # Lowshelf's wide RBJ transition is not at full depth there either.
        # Measured live 2026-07-24: it returned only 5.81 dB of a 9.27 dB spend
        # (raw −22.21 → seed −16.396), leaving the whole tweeter band ~3 dB low.
        # The ripple scan tried to correct it (−8.796, i.e. +13.4) and the
        # seed-anchored guard rejected that at 7.6 > 6.0 on BOTH runs, so the
        # under-returning seed shipped twice. The core-band anchor makes
        # give-back ≈ spend by construction, so the scan no longer has to fight
        # the seed and the guard no longer blocks the correction.
        #
        # PR-L3 (2026-07-27) fixed the overlap-band frame that paragraph
        # describes — `solve_branch_trims` now reads each branch on its own
        # side of Fc — but the anchor stays: the argument for it was never
        # only the band, it is that measured give-back beats any solver
        # prediction for restoring a corrected branch's own level. What DID
        # change is `raw_trim` underneath it, which carried 10.9-13.1 dB of
        # frame error into every anchored trim on the JTS3 captures.
        raw_trim = dict(cand.trim_db)
        anchored_unnormalized = {
            role: float(raw_trim.get(role, 0.0) + fits[role].correction_giveback_db)
            for role in (woofer_role, tweeter_role)
        }
        # Normalize to non-positive: a branch whose own cuts give back more than
        # its raw attenuation would otherwise land POSITIVE (a boost), which the
        # emitter refuses and the hardware must never see. Subtracting the same
        # shift from every role preserves the relative leveling exactly and is
        # honest extra ledger (a little more max-SPL spent, disclosed below).
        normalize_shift_db = max(0.0, max(anchored_unnormalized.values()))
        anchored = {
            role: value - normalize_shift_db
            for role, value in anchored_unnormalized.items()
        }

        # Ripple fine-tune around the anchor: the anchor sets the LEVEL, the
        # scan only polishes summed flatness near it. Same band/sign as before.
        #
        # ...and only where that band straddles Fc (PR-L3). This is the SAME
        # one-sided-band hazard `program_analysis._build_candidate` guards on
        # the raw candidate, reached through the SAME `overlap_band_hz` clamp
        # a few lines above, and this call site is the one whose result
        # becomes `role_attenuations_db` — the gain the emitted graph runs.
        # On a tweeter swept from Fc the band is `[Fc, 2*Fc]`, where the
        # woofer sits 20+ dB down its skirt: the summed ripple is the
        # tweeter's own and barely responds to the tweeter's gain, so the
        # scan is not measuring the handoff. It mattered less while the seed
        # was biased and the scan's pull partly cancelled it; PR-L3 unbiased
        # the seed, which is exactly what makes an unguarded pull dangerous
        # here — bounded only by LINEARIZATION_TRIM_SANITY_MARGIN_DB, it can
        # walk the applied trim up to 6 dB off a correct anchor without
        # tripping anything. A selector that cannot see the woofer does not
        # set the woofer's handoff level.
        assert analysis.alignment is not None  # MEASURE analyses always carry one
        ripple_lin: float | None = None
        if lo_clamped < self._fc_hz < hi:
            trim_t_lin, ripple_lin, _seed_lin = solve_ripple_optimal_trim(
                freqs, W_lin, T_lin, self._fc_hz,
                lo_hz=lo_clamped, hi_hz=hi,
                seed_trim_db=anchored[tweeter_role],
                trim_w_db=anchored[woofer_role],
                sign=analysis.alignment.polarity_sign,
            )
        else:
            log_event(
                logger, "correction.crossover_v2_linearization_ripple_trim_skipped",
                session_id=self.session_id,
                reason="ripple_band_one_sided",
                fc_hz=round(float(self._fc_hz), 3),
                ripple_band_hz=(round(float(lo_clamped), 1), round(float(hi), 1)),
                anchored_trim_db={k: round(v, 3) for k, v in anchored.items()},
            )
            trim_t_lin = anchored[tweeter_role]
        resolved = {
            woofer_role: anchored[woofer_role], tweeter_role: float(trim_t_lin),
        }

        log_event(
            logger, "correction.crossover_v2_linearization_giveback",
            session_id=self.session_id,
            giveback_db={
                role: round(float(fits[role].correction_giveback_db), 3)
                for role in (woofer_role, tweeter_role)
            },
            raw_trim_db={k: round(v, 3) for k, v in raw_trim.items()},
            anchored_trim_db={k: round(v, 3) for k, v in anchored.items()},
            normalize_shift_db=round(float(normalize_shift_db), 3),
            # The FIT frame's own per-role level, beside the TRIM frame this
            # line already carries (PR-L3 forensics). `raw_trim_db` should
            # track the negated difference of these two; a large disagreement
            # means the level match and the fit are measuring different
            # things, which is exactly what shipped the 10 dB-dark tweeter.
            target_level_db={
                role: round(float(fits[role].target_level_db), 3)
                for role in (woofer_role, tweeter_role)
            },
        )

        # The guard measures the SCAN's drift from the anchor — the anchor
        # itself is trusted by construction (it is measured give-back, not a
        # prediction), so only a scan that walks far from it is suspect.
        wild = (
            abs(trim_t_lin - anchored[tweeter_role])
            > LINEARIZATION_TRIM_SANITY_MARGIN_DB
        )
        # PR-L4 item 9: drift from the anchor says the scan MOVED, not that it
        # moved the wrong way — and on the 2026-07-27 session the difference
        # mattered. The scan had walked 5.500 dB, missing this guard by 0.50 dB,
        # and its walk was TOWARD a correct level; the fallback the guard would
        # have taken was 5.5 dB darker still. A guard whose rejection branch can
        # point away from the truth is not a safety net, so it no longer
        # decides: BOTH candidate pairs are graded by the one direct measurement
        # of what a trim is FOR (their realized inter-driver level, item 1) and
        # the better-levelled pair is committed.
        #
        # **Unconditionally, not only on a rejection** (PR-L4 review B2). The
        # first cut graded only inside the `wild` branch, which made the whole
        # thing non-monotonic in the drift it was supposed to police: below the
        # 6.0 dB guard `resolved` committed UNGRADED and item 1's 3.0 dB
        # tolerance then hard-stopped the session, while a LARGER drift tripped
        # the guard, fell back to the anchor, and applied. Measured by the
        # reviewer: 2.0 / 4.0 / 5.9 dB drifts refused at committed level errors
        # of -3.6 / -5.6 / -7.5 dB, and a 6.5 dB drift applied at -1.6 dB. The
        # guard's ceiling and item 1's tolerance were fighting, and the band
        # between them was a terminal failure instead of a fallback.
        #
        # **Named behaviour change**: the ripple polish now survives only when
        # it does not worsen the level match (ties go to the anchor, which is
        # level-preserving by construction). That is a real narrowing of
        # #1667's scan, and it is the intended ordering — inter-driver level is
        # the load-bearing property, summed ripple is the polish, and PR-L3
        # already skips the scan entirely on the one-sided geometry where it
        # was doing the damage. Whichever pair wins still faces item 1's
        # refusal at `_publish_measure_candidate`; drift is retained as the
        # trigger for the WARNING and as telemetry, never as the verdict.
        match_resolved = self._realized_level_match(
            freqs, W_lin, T_lin, resolved, woofer_role, tweeter_role,
            woofer_span_hz=woofer_span, tweeter_span_hz=tweeter_span,
        )
        match_anchored = self._realized_level_match(
            freqs, W_lin, T_lin, anchored, woofer_role, tweeter_role,
            woofer_span_hz=woofer_span, tweeter_span_hz=tweeter_span,
        )
        anchor_levels_better = abs(match_anchored.difference_db) <= abs(
            match_resolved.difference_db
        )
        role_attenuations_db = anchored if anchor_levels_better else resolved
        realized_match = match_anchored if anchor_levels_better else match_resolved
        # Never the RAW trim, whichever pair wins — the correction filters are
        # emitted either way, and raw trim + emitted filters is the known
        # deterministic VERIFY-mismatch class (#1668 PR-D).
        self._last_linearization_outcome = "trim_rejected" if wild else "fitted"  # SF3
        if wild:
            log_event(
                logger, "correction.crossover_v2_linearization_trim_rejected",
                level=logging.WARNING, session_id=self.session_id,
                raw_trim_db={k: round(v, 3) for k, v in raw_trim.items()},
                resolved_trim_db={k: round(v, 3) for k, v in resolved.items()},
                # #1668 anchored give-back: the anchor the guard is measured
                # against.
                anchored_trim_db={k: round(v, 3) for k, v in anchored.items()},
                # The pair actually committed. Since PR-L4 this is the anchor
                # only when the anchor LEVELS BETTER — the two numbers below say
                # which won and by how much.
                fallback_trim_db={
                    k: round(v, 3) for k, v in role_attenuations_db.items()
                },
                anchored_level_error_db=round(
                    float(match_anchored.difference_db), 3
                ),
                resolved_level_error_db=round(
                    float(match_resolved.difference_db), 3
                ),
                committed="anchored" if anchor_levels_better else "resolved",
                margin_db=LINEARIZATION_TRIM_SANITY_MARGIN_DB,
                # P4 telemetry (2026-07-24 review): the ripple at each trim lets
                # live evidence distinguish "legitimate flatter optimum rejected"
                # from "garbage correctly caught" before anyone widens the guard.
                # ``None`` is unreachable here — a skipped scan leaves the trim
                # AT the anchor, so ``wild`` is false by construction — but the
                # field stays honest rather than reporting a fabricated 0.0.
                resolved_ripple_db=(
                    round(float(ripple_lin), 3) if ripple_lin is not None else None
                ),
                raw_predicted_ripple_db=round(float(cand.predicted_ripple_db), 3),
            )

        # PR-L4 item 1: the inter-driver realized-level ledger, on every fitted
        # candidate whatever the guard decided. Recorded here (where the
        # linearized branches live) and ASSERTED in
        # ``_publish_measure_candidate`` — deliberately outside SF2's
        # degrade-to-trims-only catch, because an accountability refusal must
        # stop the session rather than quietly become the unlinearized path.
        self._last_realized_level_match = realized_match
        log_event(
            logger, "correction.crossover_v2_realized_level_match",
            level=(
                logging.WARNING if not realized_match.matched else logging.INFO
            ),
            session_id=self.session_id,
            matched=realized_match.matched,
            level_w_db=round(float(realized_match.level_w_db), 3),
            level_t_db=round(float(realized_match.level_t_db), 3),
            difference_db=round(float(realized_match.difference_db), 3),
            tolerance_db=realized_match.tolerance_db,
            woofer_band_hz=tuple(
                round(v, 1) for v in realized_match.woofer_band_hz
            ),
            tweeter_band_hz=tuple(
                round(v, 1) for v in realized_match.tweeter_band_hz
            ),
            trim_db={k: round(v, 3) for k, v in role_attenuations_db.items()},
        )

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

    def _realized_level_match(
        self,
        freqs: np.ndarray,
        w_tf: np.ndarray,
        t_tf: np.ndarray,
        trims_db: Mapping[str, float],
        woofer_role: str,
        tweeter_role: str,
        *,
        woofer_span_hz: tuple[float, float],
        tweeter_span_hz: tuple[float, float],
    ) -> RealizedLevelMatch:
        """One candidate trim pair's realized inter-driver level (PR-L4 item 1).

        A thin role-ordering adapter over
        :func:`~jasper.audio_measurement.program_analysis.realized_branch_level_match`
        — this conductor speaks roles, that estimator speaks woofer/tweeter
        branches, and nothing else belongs in between. Kept a method rather than
        a closure so the guard above can grade BOTH candidate pairs with one
        obviously-identical call.
        """
        return realized_branch_level_match(
            freqs, w_tf, t_tf, self._fc_hz,
            trim_w_db=float(trims_db[woofer_role]),
            trim_t_db=float(trims_db[tweeter_role]),
            woofer_span_hz=woofer_span_hz,
            tweeter_span_hz=tweeter_span_hz,
        )


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


def capture_progress_label(index: int, capture_target: int) -> str:
    """The ONE counter a step screen shows — "Measurement N of T".

    Server-derived and whole-session, per the flow-simplification redesign
    (§2.1). It replaces BOTH of the two counters the household used to read at
    once: the per-group "Spot i of n" that headlined each cloud entry, and the
    phone's own ``#status`` line, which counted the same walk differently and
    read as a contradiction. ``index`` is the entry's 1-based WIRE index (the
    relay's own index space), not the 0-based ``CapturePlanEntry.index``.
    """
    return f"Measurement {int(index)} of {int(capture_target)}"


def _cloud_entry_screen(
    *, progress: str, title: str, body: str, auto_advance: str,
) -> dict[str, str]:
    return {
        "progress": progress,
        "title": title,
        "body": body,
        "auto_advance": auto_advance,
    }


def build_v2_capture_plan(
    roles_bands: Sequence[RoleBand],
    fc_hz: float,
    *,
    plan_shape: V2PlanShape | None = None,
    tier: Any = None,
    cloud_measure_positions: int | None = None,
    cloud_verify_positions: int | None = None,
) -> Any:
    """The heterogeneous cloud CapturePlan (§5.7 + flat-linearization PR-3b).

    16 entries at the full tier's shipped defaults (7 for express): CHECK,
    MEASURE, ``N-1`` prompted pre-apply positions, VERIFY, ``M-1`` prompted
    post-apply positions — the layout ``build_v2_cloud_index_phase_map``
    documents, built from that same function so prompt and phase cannot
    disagree.

    **Screen grammar (flow-simplification §2.1).** Every entry's ``screen``
    carries ``progress`` (the one server-derived counter), ``title`` (ONE
    imperative instruction) and ``body`` (at most one supporting clause). The
    VERIFY entry is the deliberate exception: its ``title``/``body`` stay the
    apply-hold copy an old cached page renders during the hold, and the
    post-apply confirmation instruction rides the NEW ``confirm_title`` /
    ``confirm_body`` keys such a page ignores (§2.2). Screens are an opaque
    ``str -> str`` map, so none of this is a relay/protocol change.

    Entry durations derive from the composed programs (MEASURE sized from a
    nominal gain plan — sweep/gap lengths are gain-independent, so the duration
    is exact even before CHECK's solve) plus a lead/tail margin; each entry's
    ``screen`` carries the phase prompt AND the §5.2 auto-advance policy:
    CHECK is the session's one required tap, MEASURE auto-advances behind a
    visible cancelable countdown, VERIFY arms on the apply-complete host event,
    and every prompted cloud position requires the operator's tap — the mic has
    to be MOVED between them, so a countdown would fire into a hand still in
    flight.

    No phone-side mechanism is new: ``CapturePlanEntry.screen`` and
    ``AUTO_ADVANCE_TAP`` already carry per-entry copy the page renders and gates
    on, and the deployed page reads ``max_attempts``/``capture_target``
    generically with no plan-length cap of its own.
    """
    from jasper.capture_relay.spec import CapturePlan, CapturePlanEntry

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
    shape = _shape_from_kwargs(
        plan_shape,
        tier=tier,
        cloud_measure_positions=cloud_measure_positions,
        cloud_verify_positions=cloud_verify_positions,
    )
    index_phase = build_v2_cloud_index_phase_map(plan_shape=shape)
    target = shape.capture_target
    verify_ms = _program_duration_ms(verify) + CAPTURE_ENTRY_MARGIN_MS
    entries: list[Any] = [
        CapturePlanEntry(
            index=0,
            kind_label="check",
            duration_ms=_program_duration_ms(check) + CAPTURE_ENTRY_MARGIN_MS,
            screen={
                "progress": capture_progress_label(1, target),
                "title": (
                    "Stand the phone about 1 m in front of the speaker, at "
                    "tweeter height."
                ),
                "body": "Stay quiet — JTS listens to the room first.",
                "auto_advance": AUTO_ADVANCE_TAP,
            },
        ),
        CapturePlanEntry(
            index=1,
            kind_label="measure",
            duration_ms=_program_duration_ms(measure) + CAPTURE_ENTRY_MARGIN_MS,
            screen={
                "progress": capture_progress_label(2, target),
                "title": "Keep the phone still — this spot is the mark.",
                "body": "Measuring both drivers; you will come back here later.",
                "auto_advance": AUTO_ADVANCE_COUNTDOWN,
                "countdown_s": str(AUTO_ADVANCE_COUNTDOWN_S),
                "cancelable": "1",
            },
        ),
    ]
    # The two prompted groups. ``index_phase`` is 1-based (the relay's own
    # index space); ``CapturePlanEntry.index`` is 0-based, hence the -1.
    cloud_measure_indexes = [
        i for i, p in sorted(index_phase.items()) if p == PHASE_CLOUD_MEASURE
    ]
    cloud_verify_indexes = [
        i for i, p in sorted(index_phase.items()) if p == PHASE_CLOUD_VERIFY
    ]
    for offset, capture_index in enumerate(cloud_measure_indexes):
        prompt = CLOUD_POSITION_PROMPTS[offset]
        entries.append(
            CapturePlanEntry(
                index=capture_index - 1,
                kind_label="cloud_measure",
                duration_ms=verify_ms,
                screen=_cloud_entry_screen(
                    progress=capture_progress_label(capture_index, target),
                    title=prompt.headline,
                    body=prompt.detail,
                    auto_advance=AUTO_ADVANCE_TAP,
                ),
            )
        )
    verify_index = _index_of_phase(index_phase, PHASE_VERIFY)
    verify_screen: dict[str, str] = {
        "progress": capture_progress_label(verify_index, target),
        "title": "Applying",
        # Fallback only — the live hold shows the CaptureBeginDeferred
        # deferral's own user_message instead (authorize_begin below),
        # which wins whenever a hold is actually in progress. BOTH carry
        # the reposition instruction: the pre-apply cloud leaves
        # the operator standing at a wide offset, and VERIFY's tracking
        # comparator grades against MEASURE's design-axis prediction,
        # so it is only meaningful captured back at the mark. The apply
        # hold is exactly the window in which to walk back.
        #
        # DELIBERATELY NOT repurposed into the redesign's instruction slot
        # (§2.1/§2.2): ``validate_capture_page`` enforces a build-stamp
        # FORMAT and never a minimum build, so a phone carrying a cached
        # pre-redesign bundle is admitted and renders these two as the hold
        # heading. Repurposing them would make that page show a walk-back
        # instruction as its "Applying" heading. The post-apply confirmation
        # copy therefore rides the new keys below, which an old page ignores.
        "body": VERIFY_ANCHOR_HOLD_MESSAGE,
        "auto_advance": AUTO_ADVANCE_ON_APPLY,
        # The step-11 fix (§2.2): the tone must not fire the moment the apply
        # lands, racing the household's walk back to the mark. The entry KEEPS
        # ``AUTO_ADVANCE_ON_APPLY`` (so the runner's ``begin_budget`` hold
        # semantics stay live); what changes is that the page renders these
        # two once authorization arrives and waits for the tap before arming.
        # That wait sits in the runner's ``awaiting_arm`` phase, whose budget
        # is ``DEFAULT_TIMEOUT_S`` (120 s) — comfortably past the 60 s
        # acceptance criterion.
        "confirm_title": "Back on the mark, holding still?",
        "confirm_body": "Same spot, same height, pointed at the speaker.",
    }
    # The phone's END screen once every capture completes
    # (capture-page/js/main.js's renderPlanAllDone reads the FINAL wire
    # index's entry) — owner ruling, 2026-07-20: state the outcome plainly and
    # point at the speaker page for undo/compare, rather than the shared "All
    # measurements done" generic copy every other capture-plan flow gets.
    #
    # WHICH entry is last depends on the tier: the full plan ends on the
    # post-apply group's tail, express (M = 1) ends on VERIFY itself. Express
    # also says LESS, because it verified less — it confirmed the result at
    # the mark and made no cross-position post-apply claim at all (§1.3).
    done_screen = {
        "done_title": "Your speaker is tuned",
        "done_body": (
            "Verified and applied. Manage or undo on the speaker page."
            if shape.has_cloud_verify_group
            else "Confirmed at the mark and applied. Run a Full measurement "
            "for the verified-everywhere result, or manage this one on the "
            "speaker page."
        ),
    }
    if not shape.has_cloud_verify_group:
        verify_screen.update(done_screen)
    entries.append(
        CapturePlanEntry(
            index=verify_index - 1,
            kind_label="verify",
            duration_ms=verify_ms,
            screen=verify_screen,
        )
    )
    for offset, capture_index in enumerate(cloud_verify_indexes):
        prompt = CLOUD_POSITION_PROMPTS[offset]
        last = offset == len(cloud_verify_indexes) - 1
        screen = _cloud_entry_screen(
            progress=capture_progress_label(capture_index, target),
            title=prompt.headline,
            body=prompt.detail,
            auto_advance=AUTO_ADVANCE_TAP,
        )
        if last:
            screen.update(done_screen)
        entries.append(
            CapturePlanEntry(
                index=capture_index - 1,
                kind_label="cloud_verify",
                duration_ms=verify_ms,
                screen=screen,
            )
        )
    return CapturePlan(
        capture_target=target,
        max_attempts=shape.max_attempts,
        schema_version=2,
        entries=tuple(entries),
    )


def _index_of_phase(index_phase: Mapping[int, str], phase: str) -> int:
    for index, value in sorted(index_phase.items()):
        if value == phase:
            return index
    raise CrossoverV2FlowError(f"cloud index map has no {phase} entry")


def build_v2_verify_capture_plan(fc_hz: float) -> Any:
    """A 1-entry verify-only plan for the §5.2 re-verify re-arm session.

    Used by ``/crossover/v2/verify`` after a VERIFY fail/inconclusive when the
    original session has died: the household explicitly chose "Try again," so
    the single entry requires the tap (no countdown — apply already happened).
    The hosting conductor maps relay index 1 → VERIFY via ``index_phase_map``.

    **The copy leads with how cheap this is (§2.4).** The 2026-07-27 hardware
    session ABANDONED this recovery because the screen never said that "Try
    again" is one sweep rather than another walk — the household read it as
    re-doing the whole cloud and stopped instead. Saying the cheap thing
    loudly is the fix; nothing about the measurement changed.
    """
    from jasper.capture_relay.spec import CapturePlan, CapturePlanEntry

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
            "progress": capture_progress_label(1, 1),
            "title": REVERIFY_NO_REWALK_HEADLINE,
            "body": "Put the phone back on the mark and hold it still.",
            "auto_advance": AUTO_ADVANCE_TAP,
        },
    )
    return CapturePlan(
        capture_target=1,
        max_attempts=CAPTURE_PLAN_MAX_ATTEMPTS,
        schema_version=2,
        entries=(entry,),
    )


def build_v2_verify_session_spec(
    fc_hz: float,
    *,
    acknowledgement_binding: str,
    **spec_kwargs: Any,
) -> Any:
    """The relay v3 spec for a verify-only re-arm session (§5.2 re-verify).

    Its consent steps LEAD with :data:`REVERIFY_NO_REWALK_HEADLINE` — the same
    sentence the plan entry's own instruction carries — because the 2026-07-27
    hardware session abandoned this recovery for want of it (§2.4).
    """
    from jasper.capture_relay.spec import build_crossover_sweep_spec

    plan = build_v2_verify_capture_plan(fc_hz)
    return build_crossover_sweep_spec(
        driver_label="crossover verification",
        driver_role="summed",
        acknowledgement_binding=acknowledgement_binding,
        stimulus_duration_ms=plan.entries[0].duration_ms,
        capture_plan=plan,
        reverify_lead=REVERIFY_NO_REWALK_HEADLINE,
        **spec_kwargs,
    )


def session_wall_clock_ceiling_s(capture_plan: Any) -> float:
    """The walked-away volume ceiling for one plan, scaled by its length.

    ``session_volume_plan.DEFAULT_WALL_CLOCK_CEILING_S`` (1800 s ≈ 2× the relay
    TTL) was sized for the 3-entry flow. A 16-capture cloud is a genuinely
    longer session — the operator walks the mic to a new spot, reads a prompt,
    and taps, once per position — so a fixed 1800 s would force-drain the
    measurement volume mid-cloud and turn a good session into a
    volume_recovery screen.

    Scaling, not a bigger constant: the ceiling grows by
    :data:`WALL_CLOCK_CEILING_PER_ENTRY_S` for every accepted capture beyond
    the 3-entry baseline, and is hard-capped by the volume plan's own
    ``MAX_WALL_CLOCK_CEILING_S`` (which owns that bound, since it owns the
    walked-away guarantee). The per-entry number is a BUDGET ALLOWANCE, not a
    measured position time — nothing has yet timed a household walking a cloud
    (the hardware smoke after PR-4/PR-7 is where that number gets its first
    measurement); it is deliberately generous, because the failure it guards
    against is a false drain mid-session while the failure it trades against is
    a walked-away speaker returning to household volume a few minutes later
    than it might have. The restore ladder ("exact" then the -60 dBFS emergency
    floor) and the restore-once latch are untouched.

    At the Full tier's shipped 16-entry cloud this is 1800 + 13*120 = 3360 s;
    at the Express tier's 7-entry cloud, 1800 + 4*120 = 2280 s; at the
    19-entry maximum the unclamped value would be 3720 s and the plan's hard
    cap binds at 3600 s.
    """
    from jasper.active_speaker.session_volume_plan import (
        DEFAULT_WALL_CLOCK_CEILING_S,
        MAX_WALL_CLOCK_CEILING_S,
    )

    target = int(getattr(capture_plan, "capture_target", CAPTURE_PLAN_TARGET) or 0)
    extra = max(0, target - CAPTURE_PLAN_TARGET)
    return min(
        MAX_WALL_CLOCK_CEILING_S,
        DEFAULT_WALL_CLOCK_CEILING_S + extra * WALL_CLOCK_CEILING_PER_ENTRY_S,
    )


# Per accepted capture beyond the 3-entry baseline. 120 s covers a prompt read,
# a deliberate mic move, a tap, the ~16 s sweep entry, and the upload with room
# to spare. See ``session_wall_clock_ceiling_s`` for why it is generous and
# what it is NOT (a measurement).
WALL_CLOCK_CEILING_PER_ENTRY_S = 120.0

# A fixed, representative 2-way RoleBand pair for :func:`tier_display_info`
# ONLY — never the household's actual excitation ceilings/topology. See that
# function's docstring for why a representative pair is honest here. The
# tweeter's lower edge is deliberately the CONSERVATIVE end of a physically
# plausible tweeter (~1.5-2 kHz, not the 300 Hz woofer/midrange territory an
# earlier revision used) — S3 review finding, adversarial review of PR #1780:
# a too-low f1 biased the estimated sweep duration (and so the displayed
# minutes) SHORT, the wrong failure direction for a number the household
# reads as a promise.
_DISPLAY_ROLES_BANDS = (
    RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
    RoleBand("tweeter", 1, FrequencyBand(1800.0, 20000.0)),
)
_DISPLAY_FC_HZ = 1600.0


def tier_display_info() -> dict[str, dict[str, int]]:
    """Per-tier ``{capture_target, estimated_minutes}`` for the wizard's
    pre-session tier chooser (flow-simplification §1.1/§3).

    The chooser must show the SAME derived duration a live session's own
    capture plan would display, never a hand-written prettier figure
    (§1.1). But at chooser time no session exists yet, and resolving the
    household's REAL excitation ceilings/topology
    (:func:`~jasper.web.correction_crossover_v2.resolve_conductor_context`)
    is refuse-if-not-ready and can regenerate the crossover preview file as
    a side effect — wrong for a value this module computes on every ~1.5 s
    poll of the ``microphone_check`` screen, which must render the chooser
    regardless of whether that heavier resolution would currently succeed.

    **A fixed representative :class:`RoleBand` pair is honest here, but NOT
    because program length is invariant to the band (S3 fix, adversarial
    review of PR #1780 — an earlier revision of this docstring overclaimed
    that).** The realized sweep length genuinely varies with the swept
    band's edges: each sweep's MESM inter-sweep gap
    (:func:`~jasper.audio_measurement.program.mesm_gap_samples`) and its own
    Novak-synchronized sample count both depend on ``f1``/``f2`` — a
    narrower or differently-centered band realizes a measurably different
    duration, not the same one. The invariant that actually makes a fixed
    pair honest is narrower: :meth:`CapturePlan.estimated_minutes`'s
    ceil-to-whole-minutes quantum absorbs that variance across the
    PLAUSIBLE 2-way band space. Swept empirically across several genuinely
    different plausible topologies (varying woofer/tweeter bands and
    ``fc_hz`` — see ``tests/test_crossover_v2_conductor.py``'s
    ``test_tier_display_info_minutes_hold_across_plausible_topologies``),
    Full displays 11 minutes and Express displays 5 minutes in every case
    checked, with Express the tighter margin (on the order of 10-15 s of
    headroom before the next minute boundary, at this representative pair —
    the number that would need re-deriving if a future change genuinely
    widened the plausible band space). ``capture_target`` needs no audio
    program at all — it is pure arithmetic on the resolved
    :class:`V2PlanShape`.

    **Memoized (N1 fix, adversarial review of PR #1780).** The representative
    inputs are fixed module constants, so the result never changes within a
    process — computing it fresh cost 4 :func:`build_v2_capture_plan` calls
    per envelope render (:func:`~jasper.active_speaker.crossover_envelope_v2._tier_choice_actions`
    calls this once per tier action) on every ~1.5 s wizard poll, ~8 ms on a
    fast Mac and worse on a Pi 5. :func:`functools.lru_cache` does not cache
    an exception, so a genuine regression in the representative build would
    otherwise re-raise on every poll forever; the try/except below is a
    one-time fallback specifically for that residual path (N5b), not a
    per-poll retry.
    """
    try:
        return _tier_display_info_cached()
    except (CrossoverV2FlowError, ValueError) as exc:
        log_event(
            logger, "correction.tier_display_info_failed",
            level=logging.WARNING, error=str(exc),
        )
        return {
            tier: {
                "capture_target": resolve_plan_shape(tier).capture_target,
                "estimated_minutes": 0,
            }
            for tier in TIERS
        }


@lru_cache(maxsize=1)
def _tier_display_info_cached() -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for tier in TIERS:
        shape = resolve_plan_shape(tier)
        plan = build_v2_capture_plan(
            _DISPLAY_ROLES_BANDS, _DISPLAY_FC_HZ, plan_shape=shape,
        )
        out[tier] = {
            "capture_target": shape.capture_target,
            "estimated_minutes": plan.estimated_minutes(),
        }
    return out


def build_v2_session_spec(
    roles_bands: Sequence[RoleBand],
    fc_hz: float,
    *,
    acknowledgement_binding: str,
    plan_shape: V2PlanShape | None = None,
    tier: Any = None,
    cloud_measure_positions: int | None = None,
    cloud_verify_positions: int | None = None,
    **spec_kwargs: Any,
) -> Any:
    """One relay v3 session spec spanning every phase of a cloud session (§5.7).

    Rides the existing ``build_crossover_sweep_spec`` (same kind, transport,
    and placement-acknowledgement machinery) with the cloud plan attached, and
    passes ``guided_captures`` so the spec selects the GUIDED consent copy —
    the fixed-on-axis wording that builder emits by default promises a
    stationary mic for the whole session, which is exactly what a cloud
    breaks. The guided copy still names the mark as the starting point; the
    per-entry screens carry each prompted move from there. The spec-level
    stimulus duration is the longest entry so the per-capture deadline covers
    every phase.

    ``plan_shape`` is the ONE resolved (tier, N, M) value the caller also
    threads into :func:`build_v2_cloud_index_phase_map` — see
    :class:`V2PlanShape` for why that matters.
    """
    from jasper.capture_relay.spec import build_crossover_sweep_spec

    shape = _shape_from_kwargs(
        plan_shape,
        tier=tier,
        cloud_measure_positions=cloud_measure_positions,
        cloud_verify_positions=cloud_verify_positions,
    )
    plan = build_v2_capture_plan(roles_bands, fc_hz, plan_shape=shape)
    longest_ms = max(entry.duration_ms for entry in plan.entries)
    return build_crossover_sweep_spec(
        driver_label="crossover",
        driver_role="summed",
        acknowledgement_binding=acknowledgement_binding,
        stimulus_duration_ms=longest_ms,
        capture_plan=plan,
        # The consent surface must describe the walk, not a stationary mic —
        # the count is every capture the household is prompted through, which
        # is the plan's own target.
        guided_captures=plan.capture_target,
        # …and which INSTRUMENT that walk is, so the announcement screen can
        # say "quick tune" vs "full measurement" without the spec builder
        # re-deriving a shape it does not own (§1.4 / §2.3).
        guided_tier=shape.tier,
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
    "V2PlanShape",
    "TIER_FULL",
    "TIER_EXPRESS",
    "TIERS",
    "DEFAULT_TIER",
    "EXPRESS_CLOUD_VERIFY_POSITIONS",
    "express_cloud_measure_positions",
    "normalize_tier",
    "resolve_plan_shape",
    "tier_display_info",
    "capture_progress_label",
    "REVERIFY_NO_REWALK_HEADLINE",
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
    "CAPTURE_PLAN_MAX_ATTEMPTS",
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
    "REASON_DRIVER_LEVELS_DISAGREE",
    "REASON_CORRECTION_NOT_AN_IMPROVEMENT",
    "LINEARIZATION_TRIM_SANITY_MARGIN_DB",
    "PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB",
    "spec_report_for_predicted_sum",
]
