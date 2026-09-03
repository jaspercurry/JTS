# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The walk a session will do, decided before anything plays.

Where the microphone goes, in what order, with what words on the screen, how
many attempts each pose is allowed, and which excitation program each capture
index will run. Everything here is settled **before** the first stimulus: it is
the plan, not the walk, and nothing in this module consumes a capture, holds a
lock, or reads a session's state.

``docs/REFACTOR-TUNING-2026-08.md`` §1 maps this whole region onto ``measure``,
and its wave-3 rank 4 is what moved it here. It is one region, not two: the
plan builders at the bottom are the only consumer of the prompt table, the
position geometry and the walk shape at the top, so splitting them would have
left a package module importing the flow — which the dependency direction
forbids and ``test_no_domain_module_imports_the_host_or_the_legacy_flow``
enforces.

**Four things live here, in the order the plan is assembled:**

1. **How many positions**, and the floors and ceilings a caller may configure —
   the product's realisation of the plan's *"N≈8–12 gated sweeps at guided
   positions"*, with the wide-offset guarantee specified against the floor.
2. **The prompt table**, in the order a group walks it, plus the position
   geometry a prompt implies (:func:`position_angle_deg`,
   :func:`position_geometry`) and the reach a walk shape asks for.
3. **The plan shape** — :class:`V2PlanShape` and :func:`resolve_plan_shape` —
   which turns a tier and a caller's overrides into capture counts, and the
   attempt budget those counts imply, checked against the relay's blob-index
   capacity before a household is asked to walk anywhere.
4. **The plan itself** — :func:`build_v2_capture_plan`,
   :func:`build_v2_verify_capture_plan`, and the session specs around them:
   one entry per capture index, each carrying its phase, its program, its
   prompt and its auto-advance policy.

**It decides; it does not act.** No I/O, no session attribute, no fader, no
graph. The one side effect in the module is a journal line on the tier-display
cache, and the one refusal it raises is
:class:`~.contracts.CrossoverV2FlowError`, which moved to
:mod:`.contracts` in the same wave so that both this module and the flow could
raise the same class without one importing the other.

**Mover-agnostic (MS-17).** The plan names positions in degrees and centimetres
and writes prompts a person can read; nothing here knows whether a human or an
arm will move the microphone. The prose cites
``angle_capture`` and ``jasper.web.correction_crossover_v2`` in four places to
say who READS what this builds — those are cross-references, never imports, and
the import guard walks this module.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.measurement_geometry import METERS_PER_INCH
from jasper.audio_measurement.program import (
    BASE_STIMULUS_PEAK_DBFS,
    ExcitationProgram,
    RoleBand,
    build_check_program,
    build_measure_program,
    build_verify_program,
)
from jasper.env_load import bounded_env_float
from jasper.log_event import log_event

from . import contracts as _contracts
from . import spatial as _spatial
from .contracts import CrossoverV2FlowError
from .journey import (
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_ENTRY_BASELINE,
    PHASE_LATERAL,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from .programs import (
    PILOT_LEVEL_DELTA_DB,
    courtesy_prelude_for_phase,
    measurement_band_hz,
)
from .spatial import GEOMETRY_RETRY_POSITIONS
from .sweep_spec import build_crossover_sweep_spec

logger = logging.getLogger(__name__)


CAPTURE_PLAN_TARGET = 3

# This flow's own capture retry budget: the total admission attempts a v2
# session may spend across its entries, including retaken captures.
#
# It is deliberately NOT `capture_protocol.MAX_CAPTURE_PLAN_ATTEMPTS`. Both
# builders below passed that ceiling verbatim while the two happened to be
# equal, which silently conflated a SANITY ceiling (the largest attempt
# budget any plan may ever declare, `CapturePlan.max_attempts` validation
# enforces it) with a POLICY choice (how many retakes this measurement offers
# a household). Separating them means changing this constant is a product
# decision about retries, never a consequence of the sanity ceiling moving.
CAPTURE_PLAN_MAX_ATTEMPTS = 8


# --------------------------------------------------------------------------- #
# position-group choreography (flat-linearization PR-3b)
# --------------------------------------------------------------------------- #
#
# docs/historical/linearization-campaign-2026-07.md fundamental 1: "Spatial multi-capture is THE
# measurement... N≈8–12 gated sweeps at guided positions (≥10 cm spread for HF
# null decorrelation; ≥~30 cm spread to support the LF edge)". These constants
# are the product's realisation of that fundamental.

# Re-exported from :mod:`jasper.active_speaker.crossover_v2.contracts`,
# which owns it.
DEFAULT_CLOUD_MEASURE_POSITIONS = _contracts.DEFAULT_CLOUD_MEASURE_POSITIONS
# The floor a caller may configure. Below 6 the cloud stops decorrelating HF
# nulls well enough to be worth the extra session minutes, and
# ``CLOUD_POSITION_PROMPTS``' wide-offset guarantee (below) is specified
# against exactly this number.
MIN_CLOUD_MEASURE_POSITIONS = 6
# The ceiling a caller may configure. Sized so the worst-case plan still fits
# under `capture_protocol.MAX_CAPTURE_PLAN_ATTEMPTS` — `CapturePlan.max_attempts`
# validation (sweep_spec.py) is what enforces that fit at build time.
#
# **12 → 11 when #2291's entry baseline landed**, because that claim is what
# this number IS, and one more stage-1 entry left the old value one attempt
# over. The arithmetic, spelled out for the WALK-ARMED case — which is the one
# that binds, and the reason this stays 11 through the lateral pause:
#
#     cloud_plan_max_attempts(N, M=6)                  = 1 + N + 6 + 2 + 5
#     + len(LATERAL_POSE_PROMPTS)                      = 6
#     + the entry baseline                             = 1
#     ------------------------------------------------------------------
#     N=12 -> 33, N=11 -> 32 = MAX_CAPTURE_PLAN_ATTEMPTS
#
# **There is no slack, and the table above is why.** No stage-1 plan builds that
# walk any more, but an operator's staged angle walk adds its six poses to any
# session unconditionally, through this same attempt budget — so the
# walk-armed row IS the binding one: at N=11, M=6 it lands on 32, which is
# ``MAX_CAPTURE_PLAN_ATTEMPTS`` exactly.
#
# **That row is the shipped one since the 2026-08-24 geometry ruling**, which
# put the design axis into the post-apply pose set and took
# ``DEFAULT_CLOUD_VERIFY_POSITIONS`` from 5 to 6. It used to be 31 at M=5 — one
# under — and this comment used to say raising N would spend that last one.
# The ruling spent it on a capture instead. Nothing here needs to move (32 is
# the ceiling, not one past it), but the headroom is now zero AT THIS BOUND —
# and the bound is a deliberately conservative CROSS-STAGE sum, not a
# session's real draw: stage 1 and stage 2 each build against their own
# attempt budget, so the largest single session this flow can be configured
# into is 26 of 32 and the shipped one draws 30 across both. Read "zero" as
# "the guard has no slack left", never as "the next entry must be bought with
# a household-visible retake" — a producer that genuinely needs one should
# first check whether it lands inside a single stage's own draw. The two ways
# to pay are unchanged — a step of configuration headroom (this constant), or
# a household-visible retake (``CLOUD_RETAKE_ALLOWANCE``).
#
# Nothing shipped changed when this came down: ``DEFAULT_CLOUD_MEASURE_POSITIONS``
# is 9 and stage 1 does not run the pre-apply cloud at all
# (``STAGE1_INCLUDES_CLOUD_MEASURE``). What was spent then was one step of
# configuration headroom, the cheaper of the two ways to pay named above.
MAX_CLOUD_MEASURE_POSITIONS = 11
# Total MIC POSITIONS in the post-apply cloud, VERIFY's anchor included — so
# the plan emits ``M − 1`` additional prompted positions after VERIFY, and the
# group combines ``M − 1`` curves (see the positions-are-not-curves note
# above: VERIFY's own summed capture is consumed by the tracking verdict, which
# is a different question than "is the speaker flat"). Smaller than the
# pre-apply cloud on purpose: the post-apply pass grades a correction the
# pre-apply cloud already constrained, and it is paid at the END of a long
# session where operator patience is the binding resource.
#
# It sits AT :data:`MIN_CLOUD_VERIFY_POSITIONS`, so the shipped walk is exactly
# the shape the floor already validates — the anchor plus every pose in
# :data:`CLOUD_VERIFY_POSE_PROMPTS`, both wide offsets included.
#
# **6 since the 2026-08-24 geometry ruling** (5 between the 2026-08-18 trim and
# it), because the extra capture is the DESIGN AXIS —
# :data:`CLOUD_VERIFY_POSE_PROMPTS` is where that ruling is recorded and is the
# only place to read it.
#
# Pinned equal to ``1 + len(CLOUD_VERIFY_POSE_PROMPTS)`` by an import-time guard
# beside that table: the number is declared here, where every reader of the
# other choreography constants looks, and CHECKED where the table it counts is
# defined.
DEFAULT_CLOUD_VERIFY_POSITIONS = 6
# The floor a caller may configure for the POST-apply group. It exists for the
# same reason ``MIN_CLOUD_MEASURE_POSITIONS`` does and is enforced the same way:
# a group that stops before the second wide offset carries no ~30 cm-class
# spread at all and silently voids fundamental 1's LF-edge guarantee — which
# ``test_cloud_prompts_front_load_the_wide_offsets`` states as a property of the
# TABLE, not of the default. Until this floor existed, ``M = 2`` was accepted
# and quietly broke that claim.
#
# DERIVED from the POST-APPLY table (``_min_positions_for_two_wide_offsets``
# over :data:`CLOUD_VERIFY_POSE_PROMPTS`), never a literal: reordering the
# prompts must move the floor with them, not leave a stale number behind. It is
# a different table from the pre-apply group's since the 2026-08-24 ruling gave
# the verify its own pose set, so the two floors are derived separately rather
# than one standing in for both.
MIN_CLOUD_VERIFY_POSITIONS = 6

# Retake headroom a cloud plan carries ABOVE its entry count and its geometry
# retries. Deliberately the same ABSOLUTE spare the shipped 3-entry flow has
# always had (``CAPTURE_PLAN_MAX_ATTEMPTS - CAPTURE_PLAN_TARGET`` = 5), not the
# same RATIO: `capture_protocol.MAX_CAPTURE_PLAN_ATTEMPTS`' own sizing note
# says longer sets getting proportionally fewer retakes each "is the intended
# direction — a 21-position session that needs 11 retakes has a problem retries
# will not fix."
CLOUD_RETAKE_ALLOWANCE = CAPTURE_PLAN_MAX_ATTEMPTS - CAPTURE_PLAN_TARGET

# The bounded-retry ruling (owner, 2026-08-03, issue #2086) and the two
# initiators its pooled bound is attributed to. Re-exported from
# :mod:`jasper.active_speaker.crossover_v2.admission`, which owns the ledger


# The offset class that carries fundamental 1's LF edge. A move at or past
# this distance is "wide"; everything shorter only decorrelates HF nulls.
# DERIVED FROM THE PHYSICS, not from the copy: the parent plan's two-path
# inversion side-finding needs ~30 cm-class spread before the cloud's LF
# common-mode bounce lift starts converging, and ~10 cm before HF nulls
# decorrelate at all (:data:`MIN_CLOUD_OFFSET_CM`). Because
# :attr:`CloudPositionPrompt.wide` is computed from this constant rather than
# hand-set per row, an editor who narrows a wide prompt's distance moves the
# derived group floors with it and
# ``test_cloud_prompts_front_load_the_wide_offsets`` fails loudly — the
# adjustment is refused by construction, not by a second copy of the rule.
WIDE_OFFSET_MIN_CM = 30.0
# The shortest prompted move that is still a move: below this the position is
# not decorrelating anything and is costing a household a session minute.
MIN_CLOUD_OFFSET_CM = 10.0
# How far the geometry-locked retake rungs ask the operator to go. Past every
# position in the table (its widest is 60 cm), because a retake at a distance
# the cloud already sampled is not a wider spot; and no further than that,
# because a desk-scale setup has to be able to reach it — issue #1874's open
# question is whether the lock's threshold suits tabletop clouds at all, and
# this retake must not answer it by walking a household out of the room.
GEOMETRY_RETRY_OFFSET_CM = 75.0

# The prompted-position role vocabulary. Re-exported from
# :mod:`jasper.active_speaker.crossover_v2.spatial`, which owns it beside
# the geometry a retained take records.
POSITION_ROLE_ONAX = _spatial.POSITION_ROLE_ONAX
POSITION_ROLE_OFFAX = _spatial.POSITION_ROLE_OFFAX
POSITION_ROLE_XOVR = _spatial.POSITION_ROLE_XOVR
POSITION_ROLES = _spatial.POSITION_ROLES


def format_position_distance(offset_cm: float) -> str:
    """One prompted distance, in inches with the metric value beside it.

    Units are a RECORDED OWNER RULING being superseded by a newer one. The
    body-part register (hand-widths, forearms) came from the 2026-07-25 studio
    session, where numeric prompts had proved unusable; the 2026-07-28 field
    session on issue #1805 withdrew it — *"drop body-part units — prompts
    should use inches and/or meters"*. The newer field ruling wins, and both
    units ride every prompt because a household reading this is standing in a
    room with whatever tape measure it owns.

    Centimetres rather than metres at this scale: every prompted move is
    between 0.1 m and 0.6 m, and "0.12 m LEFT" is worse copy than "12 cm
    LEFT" for the same number.
    """
    inches = round(float(offset_cm) / (METERS_PER_INCH * 100.0))
    return f"{inches:g} in ({float(offset_cm):g} cm)"


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

    ``offset_cm`` is the DISTANCE FROM THE MARK the pose states, and it is the
    row's load-bearing datum rather than decoration: ``wide`` is computed from
    it (see :data:`WIDE_OFFSET_MIN_CM`), so the ~30 cm-class guarantee cannot
    be voided by editing copy alone. ``role`` names the question the position
    answers (:data:`POSITION_ROLES`).

    **Exactly which distance, because a campaign got this wrong.** It is the
    pose's SIDEWAYS displacement measured in the mark's own plane — the
    perpendicular leg of a right triangle whose other leg is
    :data:`MARK_DISTANCE_M`, which is why :func:`position_angle_deg` converts
    it with ``atan(offset / mark distance)`` and
    :func:`~jasper.active_speaker.angle_capture.pose_at_angle` inverts it with
    ``tan``. It says WHERE a pose is relative to the design axis; it says
    NOTHING about how the microphone got there. A rig with a measurement arm
    ROTATES to the bearing rather than carrying the capsule sideways, and the
    2026-08 new-horn campaign read this field's centimetres as a carry — which
    is the misreading the pose record's own ``position_deg`` /
    ``position_axis`` fields
    (:class:`~jasper.active_speaker.crossover_v2.spatial.PositionGeometry`)
    now close.

    ``CLOUD_POSITION_PROMPTS`` is ORDERED to put two wide moves inside the
    first ``MIN_CLOUD_MEASURE_POSITIONS - 1`` offsets — pinned by test, because
    an editor reordering this table for readability would silently delete the
    LF half of the measurement.
    """

    headline: str
    detail: str = ""
    offset_cm: float = 0.0
    role: str = POSITION_ROLE_ONAX
    #: Which side of the design axis a LATERAL row sits on: ``-1`` LEFT,
    #: ``+1`` RIGHT, ``0`` for an at-mark or vertical row. Machine-readable
    #: because :func:`position_angle_deg` has to SIGN the bearing, and the only
    #: other statement of the side is the word "LEFT"/"RIGHT" inside
    #: ``headline`` — reading a bearing out of rendered copy is exactly the
    #: drift this table's derived ``wide`` property exists to avoid. Set by
    #: :func:`_pose` from the row's own ``side`` bearing, never by hand.
    lateral_sign: int = 0
    #: Which side of mark HEIGHT a row sits on: ``-1`` BELOW, ``+1`` ABOVE,
    #: ``0`` for a row that asks for no raise or lower. The elevation twin of
    #: ``lateral_sign``, and machine-readable for the same reason: the only
    #: other statement of the direction is the word "ABOVE"/"BELOW" inside
    #: ``headline``. Set by :func:`_pose` from the row's own ``updown`` bearing.
    vertical_sign: int = 0
    #: How far above (or below) mark height the row asks for, in centimetres —
    #: the elevation twin of ``offset_cm``, and a SEPARATE field rather than a
    #: re-reading of it because a COMPOUND row moves two different distances at
    #: once (the second geometry-retake rung goes 75 cm sideways AND 30 cm up).
    #: One shared magnitude would have to state one of them wrongly.
    #:
    #: ``0`` means the row asks for no raise, which is a true statement about
    #: every lateral row. A row that DOES ask for one must set it, or its record
    #: claims mark height for a pose the operator raised.
    vertical_offset_cm: float = 0.0

    @property
    def wide(self) -> bool:
        """Whether this move carries the plan's ~30 cm-class LF-edge offset.

        Derived, never stored: a row whose distance is edited below the class
        stops being wide in the same edit, which is what makes the floors
        below re-derive instead of going stale.
        """
        return float(self.offset_cm) >= WIDE_OFFSET_MIN_CM

    @property
    def at_mark(self) -> bool:
        """Whether the pose asks for no move at all — on EITHER axis.

        Derived for the same reason ``wide`` is: a raised pose is not at the
        mark, and a reader computing this from ``offset_cm`` alone would bank
        one as if it were.
        """
        return float(self.offset_cm) == 0.0 and float(self.vertical_offset_cm) == 0.0

    @property
    def text(self) -> str:
        """Headline + detail as one string — the evidence sidecar's ``prompt``.

        The sidecar is the only durable statement of WHERE a curve was
        measured, so it records the complete instruction, not the screen's
        headline slot alone.
        """
        return f"{self.headline} {self.detail}".strip() if self.detail else self.headline


# The sign convention for a horizontal bearing, in ONE place: negative is LEFT
# of the design axis, positive is RIGHT, as seen from the microphone looking at
# the speaker — the same viewpoint the prompt copy is written from, so a row
# that SAYS "LEFT" cannot be signed RIGHT.
_LATERAL_SIGNS = {"LEFT": -1, "RIGHT": 1}

# The same convention for ELEVATION, in the same one place: negative is BELOW
# mark height, positive is ABOVE, so a row that SAYS "ABOVE" cannot be signed
# down. The words are the ``updown`` slot ``_VERTICAL_POSE`` fills.
_VERTICAL_SIGNS = {"BELOW": -1, "ABOVE": 1}

# The same table read the other way, for copy generated FROM a sign rather than
# parsed into one -- inverted so ABOVE cannot be signed down in one of two
# places.
_VERTICAL_WORDS = {sign: word for word, sign in _VERTICAL_SIGNS.items()}


def _pose(
    template: str,
    offset_cm: float,
    role: str,
    detail: str = "",
    **bearing: str,
) -> CloudPositionPrompt:
    """One table row: an ABSOLUTE pose whose copy is generated from its number.

    ``template`` carries a ``{d}`` slot filled by
    :func:`format_position_distance` plus the bearing words the row supplies,
    so a row's stated distance and its ``offset_cm`` cannot drift apart — the
    number is the source and the sentence is derived from it. Two shared
    templates rather than eleven hand-written sentences is also what keeps
    every row an absolute pose: there is no per-row prose in which a relative
    delta could reappear.

    Refuses at IMPORT TIME below the HF-decorrelation floor, for the same
    reason ``wide`` is derived: a move too short to decorrelate anything is a
    session minute spent on nothing, and the floor is enforced rather than
    documented.

    ``ValueError`` rather than :class:`CrossoverV2FlowError` deliberately —
    the table below is built while this module is still executing, and that
    class is not defined until much further down, so raising it here would give
    the editor this guard exists for a ``NameError`` instead of the message.
    """
    if float(offset_cm) < MIN_CLOUD_OFFSET_CM:
        raise ValueError(
            f"a prompted cloud move must be at least {MIN_CLOUD_OFFSET_CM:g} cm "
            f"to decorrelate HF nulls, got {offset_cm:g} cm"
        )
    if role not in POSITION_ROLES:
        raise ValueError(
            f"cloud position role must be one of {POSITION_ROLES}, got {role!r}"
        )
    vertical_sign = _VERTICAL_SIGNS.get(str(bearing.get("updown") or ""), 0)
    return CloudPositionPrompt(
        headline=template.format(
            d=format_position_distance(offset_cm), **bearing
        ),
        detail=detail,
        offset_cm=offset_cm,
        role=role,
        # Derived from the row's OWN bearing word, so the sign and the sentence
        # cannot disagree. Every table row names exactly one direction word, so
        # its single ``offset_cm`` is the one displacement it moved, and the
        # axis it says nothing about keeps the neutral 0: a lateral row moves at
        # mark height, and a vertical row is back over the mark.
        lateral_sign=_LATERAL_SIGNS.get(str(bearing.get("side") or ""), 0),
        vertical_sign=vertical_sign,
        vertical_offset_cm=offset_cm if vertical_sign else 0.0,
    )


# Every wide row's supporting clause. Stepping in as you go out keeps the
# microphone about as far from the speaker as the mark is, which is the
# equidistance precondition any later position-pair level comparison needs
# (attribution-stage plan G8) — an unequal path length makes a level
# difference distance-contaminated rather than axial.
_WIDE_LATERAL_DETAIL = (
    "Step a little toward the speaker as you go out, so you stay about as far "
    "from it as the mark is, and keep the microphone pointed at it."
)
_VERTICAL_DETAIL = "Keep the microphone pointed at the speaker."

# The prompt table, in the order a group walks it.
#
# Copy provenance and the RULING THAT SUPERSEDED IT: the validated reference is
# the S0 kit's ``_prompt_position`` table
# (captures/flat-linearization-20260725/s0-kit/s0_capture.py), whose
# hand-width/forearm language was an owner request from the 2026-07-25 studio
# session after numeric prompts ("move the mic 10 cm left") proved unusable
# standing next to a speaker holding a mic stand. The 2026-07-28 field session
# (issue #1805) withdrew that register — *"drop body-part units — prompts
# should use inches and/or meters"* — so distances are numeric again, in both
# units, and the 2026-07-25 ruling no longer governs this table. Copy stays
# hardware-blind: no horn, no JTS3, nothing that assumes a particular cabinet.
#
# EVERY ROW IS AN ABSOLUTE POSE, never a delta on the previous one (owner
# field ruling, 2026-07-29 on issue #1806): "raise one hand" then "now move two
# hands left" leaves a household guessing whether the raise survived. Each row
# states the complete target — distance, bearing, and height — measured from
# THE MARK, which is also the guidance half of issue #1874: ambiguous relative
# deltas plausibly produce the clustering that trips the geometry lock.
#
# The actor is THE MICROPHONE, not "the phone" (same owner ruling): households
# measure with a phone, a laptop, or a calibrated USB mic, and the device is
# incidental to the instruction.
#
# ONE ordered table serves both groups: the pre-apply group uses
# ``[:N - 1]`` and the post-apply group ``[:M - 1]``, so whichever group ends
# soonest still gets the front-loaded spread. That is why the FIRST TWO wide
# moves sit at offsets 3 and 4 (1-based) rather than at the end, where the S0
# kit (which always ran all ten) could afford to put them — the later rows
# carry wide offsets too, but only a group long enough to reach them walks
# them, so they cannot be what the guarantee rests on. The left/right
# alternation is deliberately UNCHANGED: reordering this table moves two
# derived numbers (``MIN_CLOUD_VERIFY_POSITIONS`` and
# ``express_cloud_measure_positions()``), and what made the alternation read as
# weird in the field was the ambiguous relative phrasing, which the absolute
# poses above remove.
_LATERAL_POSE = "Move the microphone {d} to the {side} of the mark, at mark height."
_VERTICAL_POSE = "Move the microphone back over the mark, {d} {updown} mark height."

CLOUD_POSITION_PROMPTS: tuple[CloudPositionPrompt, ...] = (
    _pose(_LATERAL_POSE, 12.0, POSITION_ROLE_ONAX, side="LEFT"),
    _pose(_LATERAL_POSE, 12.0, POSITION_ROLE_ONAX, side="RIGHT"),
    _pose(
        _LATERAL_POSE, 40.0, POSITION_ROLE_OFFAX,
        side="LEFT", detail=_WIDE_LATERAL_DETAIL,
    ),
    _pose(
        _LATERAL_POSE, 40.0, POSITION_ROLE_OFFAX,
        side="RIGHT", detail=_WIDE_LATERAL_DETAIL,
    ),
    _pose(
        _VERTICAL_POSE, 12.0, POSITION_ROLE_XOVR,
        updown="ABOVE", detail=_VERTICAL_DETAIL,
    ),
    _pose(
        _VERTICAL_POSE, 12.0, POSITION_ROLE_XOVR,
        updown="BELOW", detail=_VERTICAL_DETAIL,
    ),
    _pose(_LATERAL_POSE, 25.0, POSITION_ROLE_ONAX, side="LEFT"),
    _pose(_LATERAL_POSE, 25.0, POSITION_ROLE_ONAX, side="RIGHT"),
    _pose(
        _LATERAL_POSE, 60.0, POSITION_ROLE_OFFAX,
        side="LEFT", detail=_WIDE_LATERAL_DETAIL,
    ),
    _pose(
        _VERTICAL_POSE, 40.0, POSITION_ROLE_XOVR,
        updown="ABOVE", detail=_VERTICAL_DETAIL,
    ),
    _pose(
        _VERTICAL_POSE, 40.0, POSITION_ROLE_XOVR,
        updown="BELOW", detail=_VERTICAL_DETAIL,
    ),
)

# --- R16 lateral evidence (plan §4.4) --------------------------------------- #
#
# §4.4: "the smallest direction reuses the existing ±12 cm and ±40 cm left/right
# moves". So the walk is DERIVED from the table above rather than restating its
# copy — and by PREDICATE, not by slice index, so reordering that table for
# readability cannot silently swap which poses the lateral walk asks for.
_LATERAL_POSE_OFFSETS_CM = (12.0, 40.0)

# The walk OPENS and CLOSES at the mark. Both rows bypass ``_pose``: its
# ≥ MIN_CLOUD_OFFSET_CM floor guarantees a prompted move DECORRELATES HF nulls,
# and these two exist to CORRELATE with each other.
#
# Why an at-mark pose at all, when the anchor MEASURE just ran there: the
# anchor's evidence is COMPOSED to the configured crossover the moment it is
# analyzed (§4.2), while a pose is kept neutral so the consumer can compose it
# per candidate. The two are not comparable curves, so a drift bracket drawn
# between them would be comparing a composition to its absence. One extra sweep
# buys a design-axis sample in the SIDES' own fidelity class and makes the
# closing bracket an exact same-instrument repeat of the opening one.
LATERAL_MARK_PROMPT = CloudPositionPrompt(
    headline="Leave the microphone on the mark — one more sweep from here.",
    detail="Nothing to move yet.",
    offset_cm=0.0,
    role=POSITION_ROLE_ONAX,
)
LATERAL_MARK_RETURN_PROMPT = CloudPositionPrompt(
    headline="Last one: put the microphone back on the mark.",
    detail="Same spot, same height, pointed at the speaker.",
    offset_cm=0.0,
    role=POSITION_ROLE_ONAX,
)

# The four SIDE poses both angle walks are made of, derived from the cloud
# table by PREDICATE (see ``_LATERAL_POSE_OFFSETS_CM``). Named once because two
# walks now spend it — the R16 lateral walk below and the post-apply verify
# walk further down — and two independent comprehensions over the same
# predicate would be two places to edit and one place to forget.
_SIDE_POSE_PROMPTS: tuple[CloudPositionPrompt, ...] = tuple(
    prompt for prompt in CLOUD_POSITION_PROMPTS
    if prompt.role != POSITION_ROLE_XOVR
    and float(prompt.offset_cm) in _LATERAL_POSE_OFFSETS_CM
)

LATERAL_POSE_PROMPTS: tuple[CloudPositionPrompt, ...] = (
    (LATERAL_MARK_PROMPT,)
    + _SIDE_POSE_PROMPTS
    + (LATERAL_MARK_RETURN_PROMPT,)
)

# Import-time guard, same register as ``_pose``'s: the derivation above must
# yield exactly one LEFT and one RIGHT at each declared offset, bracketed by the
# two at-mark poses. A cloud-table edit that drops or duplicates one of those
# four fails the import rather than shipping a lopsided walk whose left/right
# disagreement term is meaningless.
if len(LATERAL_POSE_PROMPTS) != 2 * len(_LATERAL_POSE_OFFSETS_CM) + 2:
    raise ValueError(
        "the lateral walk must derive exactly one LEFT and one RIGHT pose at "
        f"each of {_LATERAL_POSE_OFFSETS_CM} cm, bracketed by the two at-mark "
        f"poses, got {len(LATERAL_POSE_PROMPTS)} poses"
    )

# --- the POST-APPLY walk's own pose set -------------------------------------- #
#
# **The design axis is a MEMBER of this walk, not just the anchor in front of
# it** (owner ruling, 2026-08-24). VERIFY's anchor is measured at the mark, but
# its summed capture is consumed by the TRACKING verdict and never joins the
# group (see ``DEFAULT_CLOUD_VERIFY_POSITIONS``' own note) — so before this
# table the post-apply group combined four off-axis curves and banked no
# on-axis position record at all. The 2026-08 new-horn campaign paid for that
# directly: it had to improvise an extra minimal MEASURE round just to get one
# on-axis summed response of the graph it had applied.
#
# The at-mark pose earns its sweep for the same reason ``LATERAL_MARK_PROMPT``
# does one group earlier: an anchor's evidence answers a different question, so
# a design-axis sample in the SIDES' own fidelity class is a curve the group can
# actually put beside them.
#
# What it costs is the fifth pose the 2026-08-18 trim gave up — ``12 cm ABOVE``,
# the journey's only above/below-mark-height sample. That ruling spent this
# capture on shortening the session; this one spends the same capture on the one
# place the household sits. No claim reads the vertical axis on its own (the
# group is combined into ONE curve and graded as a spatial average), and the
# design axis is where every chart's reference is drawn.
#
# DERIVED from the same ``_SIDE_POSE_PROMPTS`` the lateral walk uses, so an edit
# to the shared offsets moves both walks together, and vertical-free BY
# CONSTRUCTION — which is what lets ``remote_cloud_verify_positions`` stop
# clamping and lets an external positioner walk the whole thing.
#
# The at-mark row bypasses ``_pose`` for the same mechanical reason
# ``LATERAL_MARK_PROMPT`` does — a 0 cm move cannot clear
# :data:`MIN_CLOUD_OFFSET_CM` — but NOT for the same purpose, so the exemption
# is argued rather than inherited. That floor guarantees a prompted move
# DECORRELATES HF nulls, and it is a property of the GROUP, not of each row:
# the four sides beside this one carry the whole ±7/±22 spread the combine
# needs, and a fifth member on the design axis samples an arrival geometry
# none of them has rather than repeating one of them.
VERIFY_MARK_PROMPT = CloudPositionPrompt(
    headline="Stay on the mark — one sweep from here first.",
    detail="Same spot, same height, pointed at the speaker.",
    offset_cm=0.0,
    role=POSITION_ROLE_ONAX,
)

CLOUD_VERIFY_POSE_PROMPTS: tuple[CloudPositionPrompt, ...] = (
    (VERIFY_MARK_PROMPT,) + _SIDE_POSE_PROMPTS
)

# Import-time guard, same register as the lateral walk's above: the shipped
# post-apply group is the anchor plus this table, so a table edit that did not
# move ``DEFAULT_CLOUD_VERIFY_POSITIONS`` with it would silently walk a prefix
# and drop the poses past it.
if DEFAULT_CLOUD_VERIFY_POSITIONS != 1 + len(CLOUD_VERIFY_POSE_PROMPTS):
    raise ValueError(
        "the post-apply group is VERIFY's anchor plus every pose in "
        f"CLOUD_VERIFY_POSE_PROMPTS, so DEFAULT_CLOUD_VERIFY_POSITIONS must be "
        f"{1 + len(CLOUD_VERIFY_POSE_PROMPTS)}, not "
        f"{DEFAULT_CLOUD_VERIFY_POSITIONS}"
    )

# --- remote tier: the same walk, stated as ANGLES (external positioner) ------ #
#
# Re-exported from :mod:`jasper.active_speaker.crossover_v2.spatial`,
# which owns it beside the pose it turns into a bearing.
MARK_DISTANCE_M = _spatial.MARK_DISTANCE_M


def position_angle_deg(prompt: CloudPositionPrompt) -> int:
    """The signed horizontal bearing of one lateral pose, in WHOLE degrees.

    DERIVED from ``offset_cm`` and :data:`MARK_DISTANCE_M`, never tabulated: the
    remote tier walks the very same poses the hand-walked tiers do, so an edit
    to the offsets must move the angles with them instead of leaving a second,
    silently-stale table of numbers.

    **The convention, stated exactly, because a positioner acts on it.** The
    angle is the bearing from the speaker to the pose's stated LATERAL OFFSET,
    measured in the mark's own plane — ``atan(offset / mark distance)`` — with
    :data:`_LATERAL_SIGNS`' sign, so ``-7`` is 7° to the LEFT of the design
    axis. At the shipped offsets that is ±7° (12 cm) and ±22° (40 cm).

    Whole degrees, because that is the resolution the number is honest at: the
    offsets it comes from are tape-measure distances to a mark placed "about"
    1 m out, and a tenth of a degree here would claim a precision the placement
    never had. A positioner swinging at a constant radius is equidistant BY
    CONSTRUCTION — the property ``_WIDE_LATERAL_DETAIL`` asks a walking human to
    approximate — so it lands on the wide poses' intended arc rather than on the
    chord a hand-walked session settles for.

    **OPEN QUESTION — do not read the paragraph above as settled**
    (`#2932 <https://github.com/jaspercurry/JTS/issues/2932>`_). The conversion
    here is a TANGENT construction: it places the pose ``offset_cm`` sideways at
    the mark's axial distance, which puts the capsule at ``mark / cos(θ)`` —
    1.078 m at 22°, not a constant radius. Whether the rig actually swings a
    constant-radius arc is a physical fact about the hardware that no code read
    can settle, and the owner's tape measure decides it. Until then, treat the
    bearing as sound and the equidistance claim as unverified.

    Refuses a vertical row rather than returning ``0``: :data:`POSITION_ROLE_XOVR`
    has no horizontal bearing at all, and a silent zero would aim a positioner at
    the mark while the plan believed it had sampled the crossover axis.
    """
    if prompt.role == POSITION_ROLE_XOVR:
        raise CrossoverV2FlowError(
            "a vertical position has no horizontal bearing: an external "
            "positioner cannot raise or lower the microphone, so an "
            f"externally positioned walk must contain no {POSITION_ROLE_XOVR} "
            "pose (see remote_cloud_verify_positions)"
        )
    if float(prompt.offset_cm) != 0.0 and prompt.lateral_sign == 0:
        # An off-axis pose that never declared WHICH side it is on. Without this
        # the sign multiplies out and the pose reads back as 0° — "already on
        # the design axis" — so a driver would be told to stay put for a capture
        # the plan believes is off-axis, and the evidence would record an offset
        # the microphone never had. Every table row gets its sign from
        # :func:`_pose`; a row that bypassed it is a construction bug, and
        # silence here is exactly the mis-attribution the gate exists to stop.
        raise CrossoverV2FlowError(
            f"a lateral position {float(prompt.offset_cm):g} cm off the mark "
            "declares no side, so it has no signed bearing — build it through "
            "_pose (or set lateral_sign) rather than letting it read as 0°"
        )
    radians = math.atan2(float(prompt.offset_cm) / 100.0, MARK_DISTANCE_M)
    return int(round(prompt.lateral_sign * math.degrees(radians)))


def position_elevation_deg(prompt: CloudPositionPrompt) -> int:
    """The signed ELEVATION of one pose above mark height, in WHOLE degrees.

    :func:`position_angle_deg`'s twin: the same ``atan(displacement / mark
    distance)`` against the same :data:`MARK_DISTANCE_M`, over the row's OWN
    ``vertical_offset_cm``. It reads that field rather than ``offset_cm``
    because a compound row moves both ways at once and the two distances differ
    — see :attr:`CloudPositionPrompt.vertical_offset_cm`.

    **Total, and it refuses nothing.** A row that asks for no raise or lower
    signs ``0``, and 0 is TRUE of it: the pose is at mark height. That is why
    this returns an ``int`` where the horizontal helper returns ``int`` only
    after refusing two shapes — an unstated elevation has an honest zero, an
    unstated bearing does not (see
    :class:`~jasper.active_speaker.crossover_v2.spatial.PositionGeometry`).

    The rig cannot swing in elevation and no automation is asked to: a vertical
    pose is performed by a person raising the microphone by hand, and this
    states where they were asked to raise it to.
    """
    if prompt.vertical_sign == 0:
        return 0
    radians = math.atan2(float(prompt.vertical_offset_cm) / 100.0, MARK_DISTANCE_M)
    return int(round(prompt.vertical_sign * math.degrees(radians)))


def position_geometry(prompt: CloudPositionPrompt) -> _spatial.PositionGeometry:
    """One pose's WHERE, as the four fields its retained record carries.

    The single derivation behind
    :class:`~jasper.active_speaker.crossover_v2.spatial.PositionGeometry`, so
    the bearing a positioner is aimed at and the bearing a record states come
    from one place. Composes what already existed rather than adding a second
    opinion: the horizontal angle is :func:`position_angle_deg`'s, the
    elevation is :func:`position_elevation_deg`'s, the reference length is
    :data:`MARK_DISTANCE_M`.

    **Total, and that is load-bearing.** :func:`position_angle_deg` REFUSES two
    shapes, and this runs on the retention path, where "a full disk must not
    turn a good capture into a retake" — a derivation that raised would fail a
    capture the household already gave. So each refusal becomes a recorded
    ``degrees=None`` instead. ``None`` is the honest answer for both; a 0 would
    read as "on the design axis", which is precisely what neither pose is.

    The two, and they are the two the angle helper names:

    * a :data:`POSITION_ROLE_XOVR` row — a vertical prompt asks for a raise or
      a lower, so no HORIZONTAL bearing was ever commanded. Where the row asked
      the microphone to go is ``vertical_deg``, which this now states rather
      than leaving to the prompt sentence;
    * a pose whose RECORD declares no side — ``lateral_sign == 0`` at a
      non-zero offset. Today that is exactly
      :data:`CLOUD_GEOMETRY_RETRY_PROMPTS`, BOTH rungs: they are built by
      :meth:`CrossoverV2Session._prompt_shown_for` outside :func:`_pose`, and
      ``_pose`` is the only thing that signs a row from its bearing word. Their
      household COPY does name a side (rung 1 LEFT, rung 2 RIGHT) — the sign is
      missing from the record, not from the instruction, which is why this
      reads ``lateral_sign`` rather than the prose. Rung 2 is a COMPOUND pose
      (sideways *and* above mark height) and states neither number: bypassing
      :func:`_pose` costs it ``vertical_sign`` as well.
    """
    elevation = position_elevation_deg(prompt)
    if prompt.role == POSITION_ROLE_XOVR:
        return _spatial.PositionGeometry(
            axis=_spatial.POSITION_AXIS_VERTICAL,
            degrees=None,
            mark_distance_m=MARK_DISTANCE_M,
            vertical_deg=elevation,
        )
    unsigned = float(prompt.offset_cm) != 0.0 and prompt.lateral_sign == 0
    return _spatial.PositionGeometry(
        axis=_spatial.POSITION_AXIS_HORIZONTAL,
        degrees=None if unsigned else position_angle_deg(prompt),
        mark_distance_m=MARK_DISTANCE_M,
        vertical_deg=elevation,
    )


#: Re-exported from :mod:`jasper.active_speaker.crossover_v2.spatial`,
#: which owns it beside :class:`PositionGeometry`.
_DESIGN_AXIS_GEOMETRY = _spatial._DESIGN_AXIS_GEOMETRY


def remote_position_prompt(prompt: CloudPositionPrompt) -> CloudPositionPrompt:
    """One hand-walked pose, restated as the ANGLE a positioner turns to.

    Same pose, same ``offset_cm``, same :data:`POSITION_ROLES` role — only the
    copy changes, so everything downstream that reads a position's role or
    distance (the wide-offset rule, the evidence sidecar, attribution) keeps
    reading exactly what Full's walk records. That is the whole point of
    deriving this instead of writing a parallel table: the remote tier is a
    different OPERATOR, not a different measurement.

    The elevation clause is additive: a pose at mark height reads exactly the
    sentence it read before elevation was sayable. The 0° verb is the one thing
    a rise changes, and it has to — "LEAVE the microphone on the design axis"
    is a stand-still instruction, so a pose that also asks for a rise would tell
    the household not to move and then to move.
    """
    degrees_ = position_angle_deg(prompt)
    elevation = position_elevation_deg(prompt)
    if degrees_ == 0:
        verb = "Leave" if elevation == 0 else "Keep"
        bearing = f"{verb} the microphone on the design axis (0°)"
        detail = f"On the mark, {MARK_DISTANCE_M:g} m out, pointed at the speaker."
    else:
        side = "LEFT" if degrees_ < 0 else "RIGHT"
        bearing = (
            f"Turn the microphone to {degrees_:+d}° "
            f"({abs(degrees_)}° {side} of the design axis)"
        )
        detail = (
            f"Keep it {MARK_DISTANCE_M:g} m from the speaker and pointed at it."
        )
    if elevation == 0:
        return replace(prompt, headline=f"{bearing}.", detail=detail)
    updown = _VERTICAL_WORDS[1 if elevation > 0 else -1]
    return replace(
        prompt,
        headline=f"{bearing}, and {abs(elevation)}° {updown} mark height.",
        detail=detail,
    )

# What the household reads during the apply hold, and the same entry's fallback
# screen body. It carries a REPOSITION instruction because the pre-apply cloud
# ends at a wide offset while VERIFY's tracking comparator is only meaningful
# back on the design axis — the hold is the walk-back window.
VERIFY_ANCHOR_HOLD_MESSAGE = (
    "Applying the measured crossover to your speaker. While that finishes, put "
    "the microphone back on the mark — same spot, same height, pointed at the "
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
#
# Carries the SAME register as the position table (issue #1805's 2026-07-28
# ruling): numeric distances in both units, absolute poses measured from the
# mark, and "the microphone" as the actor. The second rung's height is stated
# rather than left as "a little higher or lower than before" — a household
# asked to break a spatial tie should not be the one deciding which way.
CLOUD_GEOMETRY_RETRY_PROMPTS: tuple[str, ...] = (
    "Same measurement, wider spot: move the microphone "
    f"{format_position_distance(GEOMETRY_RETRY_OFFSET_CM)} to the LEFT of the "
    "mark, at mark height, still pointed at the speaker.",
    "One more, wider still: move the microphone "
    f"{format_position_distance(GEOMETRY_RETRY_OFFSET_CM)} to the RIGHT of the "
    f"mark and {format_position_distance(WIDE_OFFSET_MIN_CM)} ABOVE mark "
    "height.",
)

#: The RISE each retake rung asks for, one per rung, in centimetres — the
#: machine-readable half of the sentences above. Rung 2 is the tree's one
#: COMPOUND pose (sideways AND up), and it is built outside :func:`_pose`, so
#: without this its record would state mark height for a microphone the
#: household was told to raise. Read straight into
#: :attr:`CloudPositionPrompt.vertical_offset_cm` by the flow's
#: ``_prompt_shown_for``, so the number and the sentence come from one place.
CLOUD_GEOMETRY_RETRY_RISE_CM: tuple[float, ...] = (0.0, WIDE_OFFSET_MIN_CM)

# Import-time guard in this file's own register: a third rung added to the copy
# above without a rise beside it would silently bank mark height for whatever
# it asks for.
if len(CLOUD_GEOMETRY_RETRY_RISE_CM) != len(CLOUD_GEOMETRY_RETRY_PROMPTS):
    raise ValueError(
        "every geometry-retake rung must state the rise it asks for: "
        f"{len(CLOUD_GEOMETRY_RETRY_PROMPTS)} rungs, "
        f"{len(CLOUD_GEOMETRY_RETRY_RISE_CM)} rises"
    )


def _min_positions_for_two_wide_offsets(
    prompts: Sequence[CloudPositionPrompt] | None = None,
) -> int:
    """Smallest group size whose walked offsets include two WIDE moves.

    DERIVED from the walked table, never hardcoded: the whole point of the
    wide-offset guarantee is that it survives someone reordering that table,
    and a literal here would be the first thing to go stale if they did. A
    group of size ``g`` walks offsets ``[:g - 1]``, so the answer is one past
    the index of the second wide prompt.

    ``prompts`` defaults to :data:`CLOUD_POSITION_PROMPTS` — the PRE-apply
    group's table, and the one the express size is derived from. The post-apply
    group has walked its own table since the 2026-08-24 geometry ruling
    (:data:`CLOUD_VERIFY_POSE_PROMPTS`), so its floor passes that table rather
    than inheriting a number derived from a walk it no longer takes.
    """
    table = CLOUD_POSITION_PROMPTS if prompts is None else tuple(prompts)
    wide = [i for i, prompt in enumerate(table) if prompt.wide]
    if len(wide) < 2:
        raise CrossoverV2FlowError(
            "a cloud walk's table must supply at least two wide offsets — "
            "fundamental 1's LF edge needs ~30 cm-class spread"
        )
    return wide[1] + 2


# The post-apply floor's own guard, here rather than beside
# :data:`CLOUD_VERIFY_POSE_PROMPTS` only because the derivation it checks is
# defined immediately above. Same register as that table's other guard: a
# reordered pose set that left MIN_CLOUD_VERIFY_POSITIONS behind would accept a
# post-apply group with no ~30 cm-class spread in it.
if MIN_CLOUD_VERIFY_POSITIONS != _min_positions_for_two_wide_offsets(
    CLOUD_VERIFY_POSE_PROMPTS
):
    raise ValueError(
        "MIN_CLOUD_VERIFY_POSITIONS must be the smallest post-apply group "
        "whose walked poses include two wide offsets, which "
        f"CLOUD_VERIFY_POSE_PROMPTS makes "
        f"{_min_positions_for_two_wide_offsets(CLOUD_VERIFY_POSE_PROMPTS)}, "
        f"not {MIN_CLOUD_VERIFY_POSITIONS}"
    )


# What happens AFTER the walk, in one clause. The pre-apply group hands the
# household a decision; the post-apply group ends the journey. Deliberately
# promises no tune in either case — stage 1 measures, and whether anything is
# applied is the household's call on the next screen.
CLOUD_WALK_SHAPE_TAIL = "Afterwards you decide what to do about what JTS heard."
CLOUD_WALK_SHAPE_TAIL_POST_APPLY = (
    "Afterwards the speaker page shows how the tune did."
)

# The granularity the orientation's REACH is quoted at, and the reason it is
# rounded UP rather than quoted exactly.
#
# A reach that is not a true ceiling is worse than no number at all: a
# household that cleared exactly the quoted space and is then prompted past it
# has been mis-set by the one sentence meant to prevent that. TWO things push a
# position's real displacement past the offset its prompt states:
#
#   * the wide rows' equidistance step-in (``_WIDE_LATERAL_DETAIL``) — after
#     stepping toward the speaker the capsule sits on a CHORD, so a stated
#     40 cm lateral move really lands ~40.9 cm from the mark at the placement
#     copy's nominal 1 m; and
#   * ``CLOUD_GEOMETRY_RETRY_PROMPTS``, which is deliberately "past every
#     position in the table" (75 cm, and ~80.8 cm on rung 2).
#
# The first is absorbed by rounding up to the next whole step — never merely
# to the stated maximum, which is why the arithmetic below is STRICTLY
# greater. The second is NOT absorbed: inflating the everyday number to cover
# a retake most sessions never see would destroy the sentence's
# space-planning value, so the retake is acknowledged in its own short clause
# instead. ``tests/test_crossover_v2_conductor.py`` re-derives both bounds and
# fails if either stops holding.
CLOUD_WALK_REACH_ROUNDING_CM = 10.0


def cloud_walk_reach_cm(positions: int) -> float:
    """The ceiling the orientation quotes for a walk of ``positions`` captures.

    DERIVED from the same ``[:positions - 1]`` slice of
    :data:`CLOUD_POSITION_PROMPTS` the walk is prompted from, then rounded UP
    to the next whole :data:`CLOUD_WALK_REACH_ROUNDING_CM` — *strictly* up, so
    the result is never merely equal to a stated offset and therefore absorbs
    the wide rows' step-in chord. See that constant for why both halves matter.
    """
    walked = max(0, int(positions) - 1)
    if walked > len(CLOUD_POSITION_PROMPTS):
        raise CrossoverV2FlowError(
            f"cloud walk shape needs {walked} position prompts but "
            f"CLOUD_POSITION_PROMPTS supplies {len(CLOUD_POSITION_PROMPTS)}"
        )
    return cloud_walk_reach_cm_of(CLOUD_POSITION_PROMPTS[:walked])


def cloud_geometry_retry_reach_cm() -> float:
    """How far from the mark the geometry-locked retake can send the operator.

    Rung 2 is a COMPOUND pose — :data:`GEOMETRY_RETRY_OFFSET_CM` sideways *and*
    :data:`WIDE_OFFSET_MIN_CM` up — so its displacement is the hypotenuse, not
    either leg. Derived here rather than inside the test so the orientation's
    honesty clause and the prompts it is honest about read one number.
    """
    return max(
        GEOMETRY_RETRY_OFFSET_CM,
        math.hypot(GEOMETRY_RETRY_OFFSET_CM, WIDE_OFFSET_MIN_CM),
    )


def cloud_walk_shape(
    prompts: Sequence[CloudPositionPrompt], *, post_apply: bool = False,
) -> str:
    """The walk's SHAPE in one sentence, for the pre-session orientation screen.

    **This replaces the enumerated preview** (issue #1941 R1). Work order D7
    (issues #1804 + #1805) put the whole walk on the consent screen so a
    household would not discover it one prompt at a time — the right intent,
    and this keeps it. What it withdraws is D7's PRESENTATION: at the Full
    tier, a second list of TEN items (eight prompted moves plus a lead and a
    tail) totalling ~250 words, stacked under a 73-word placement block,
    before the first tone. The owner's 2026-07-30 field note is the whole
    argument — *"crazy dense with the 10 steps all spelled out. The user
    doesn't know what's gonna happen next, let alone 10 things from now."*

    So the household is told the two things that list was actually being used
    to convey — **how far from the mark this gets** (can I do this where I am
    standing?) and **that they will be prompted** (I do not need to hold every
    move in my head) — and then the walk spoon-feeds itself, one position per
    screen, which is what the per-entry screens already do.

    The distance is DERIVED from the very poses the per-entry screens are built
    from — ``prompts`` is the resolved table the caller handed its plan builder,
    not a count it might slice differently — and formatted by the same
    :func:`format_position_distance` the prompts themselves use, so the
    orientation cannot describe a reach the walk does not have. ``post_apply``
    selects stage 2's tail.

    **It took a POSITION COUNT until the 2026-08-24 geometry ruling**, and
    re-sliced ``CLOUD_POSITION_PROMPTS[:positions - 1]`` to find the moves. That
    was sound while both groups walked one table from the front; the post-apply
    group now has its own pose set (:data:`CLOUD_VERIFY_POSE_PROMPTS`) and a
    caller may hand it another, so a count is no longer enough to say where a
    walk goes. Stage 1 asks through :func:`walk_shape_for`, which owns its own
    slice.
    """
    return _walk_shape(cloud_walk_reach_cm_of(prompts), post_apply=post_apply)


def walk_shape_for(
    *, cloud_positions: int, lateral: bool,
    lateral_prompts: Sequence[CloudPositionPrompt] | None = None,
) -> str:
    """The orientation sentence for a stage-1 session's ACTUAL groups (R16).

    One sentence for whichever groups run, quoting the FURTHEST reach of any of
    them — a household needs to know how much room the whole session wants, and
    two sentences quoting two ceilings would just make them pick one.

    ``lateral_prompts`` is the walk this session actually takes; ``None`` is the
    ratified table. A taken angle walk can reach far past that table's 40 cm.
    """
    table = LATERAL_POSE_PROMPTS if lateral_prompts is None else lateral_prompts
    reach = max(
        cloud_walk_reach_cm(cloud_positions) if cloud_positions else 0.0,
        cloud_walk_reach_cm_of(table) if lateral else 0.0,
    )
    return _walk_shape(reach)


def cloud_walk_reach_cm_of(prompts: Sequence[CloudPositionPrompt]) -> float:
    """:func:`cloud_walk_reach_cm`'s rounding rule over an explicit table."""
    if not prompts:
        return 0.0
    step = CLOUD_WALK_REACH_ROUNDING_CM
    furthest = max(float(p.offset_cm) for p in prompts)
    return math.floor(furthest / step) * step + step


def _walk_shape(reach: float, *, post_apply: bool = False) -> str:
    if not reach:
        # A group with no prompted moves is not a walk and gets no shape line —
        # Express's stage 2 is one held-still sweep at the mark, whose consent
        # screen already leads with REVERIFY_NO_REWALK_HEADLINE.
        return ""
    tail = (
        CLOUD_WALK_SHAPE_TAIL_POST_APPLY if post_apply
        else CLOUD_WALK_SHAPE_TAIL
    )
    # The retake clause is CONDITIONAL on the retake actually reaching past the
    # quoted ceiling, so the sentence never carries a caveat it does not need.
    # Today it always does (75 cm / ~80.8 cm against a 50 cm walk), but a
    # narrowed retake should drop the clause rather than keep a stale one.
    beyond = (
        " though a redo can ask for one step further out,"
        if cloud_geometry_retry_reach_cm() > reach
        else ""
    )
    return (
        f"Every spot is within {format_position_distance(reach)} of the "
        f"mark,{beyond} and you will be told each one when it is time — "
        f"nothing to memorise now. {tail}"
    )


# --------------------------------------------------------------------------- #
# commission tiers (flow-simplification §1)
# --------------------------------------------------------------------------- #

# The named plan SHAPES a session can be opened with. A tier is not a
# loosened floor — it is a distinct, validated (N, M) pair with its own rules,
# so ``MIN_CLOUD_MEASURE_POSITIONS`` (the FULL tier's validated floor) never
# moves to accommodate express.
TIER_FULL = "full"
TIER_EXPRESS = "express"
# The EXTERNALLY DRIVEN tier (experimental). Full's stage-1 shape, walked by a
# mic positioner an external driver moves over HTTP instead of by a household
# moving a stand by hand — so it is the one tier whose entries auto-begin
# (:data:`AUTO_ADVANCE_COUNTDOWN`) behind a per-entry POSITION GATE, and the one
# whose positions are stated as ANGLES rather than as tape-measure offsets.
#
# **Not a household choice.** ``_tier_choice_actions`` offers exactly Full and
# Express; remote is reached only by POSTing ``{"tier": "remote"}`` to
# ``/correction/crossover/v2/session``, because consenting to it means owning a
# positioner the chooser cannot see. It is in :data:`TIERS` so
# ``normalize_tier`` admits that POST — never so a chooser can render it.
TIER_REMOTE = "remote"
TIERS = (TIER_FULL, TIER_EXPRESS, TIER_REMOTE)
DEFAULT_TIER = TIER_FULL

# Express's post-apply group: VERIFY's design-axis anchor and nothing else. An
# ``M = 1`` plan emits NO cloud-verify entries, so express makes no
# cross-position post-apply claim at all — it verifies tracking at the mark
# (``VERIFY_TOLERANCE_DB``, unchanged) and says so. See the degraded-claims
# table in docs/historical/linearization-campaign-2026-07.md §1.3.
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


def _vertical_prompt_indexes() -> list[int]:
    """Where the vertical poses sit in :data:`CLOUD_POSITION_PROMPTS`."""
    return [
        i for i, prompt in enumerate(CLOUD_POSITION_PROMPTS)
        if prompt.role == POSITION_ROLE_XOVR
    ]


def remote_cloud_measure_positions() -> int:
    """Remote's PRE-APPLY group size — Full's N, and the trip-wire that guards it.

    Remote takes Full's ``N`` because its stage 1 IS Full's stage 1. That is
    safe because :data:`STAGE1_INCLUDES_CLOUD_MEASURE` is ``False``, so the
    ``[:N - 1]`` prefix of :data:`CLOUD_POSITION_PROMPTS` — which at ``N = 9``
    DOES contain vertical rows — is never walked. That one flag is the whole
    load-bearing fact; the lateral walk is beside the point here, being lateral
    by construction whether it is armed or (as since 2026-08-18) paused.

    Flipping that flag back on would ask an external positioner for a pose it
    cannot reach. Today that surfaces as :func:`position_angle_deg` raising
    while the plan is built, which is loud but names the symptom rather than the
    cause, so this states the assumption where it can be read: the guard below
    refuses at the point the two facts stop agreeing, exactly as
    :func:`remote_cloud_verify_positions`' guard does for the verify walk.
    """
    positions = DEFAULT_CLOUD_MEASURE_POSITIONS
    verticals = _vertical_prompt_indexes()
    if STAGE1_INCLUDES_CLOUD_MEASURE and verticals and verticals[0] < positions - 1:
        raise CrossoverV2FlowError(
            "the remote tier cannot walk a pre-apply cloud of "
            f"{positions} positions: prompt {verticals[0]} is a "
            f"{POSITION_ROLE_XOVR} pose and an external positioner cannot "
            "raise or lower the microphone. Give remote its own N (the "
            "vertical-free prefix, as remote_cloud_verify_positions derives) "
            "before turning STAGE1_INCLUDES_CLOUD_MEASURE on."
        )
    return positions


def remote_cloud_verify_positions() -> int:
    """Remote's post-apply group size — DERIVED as "Full's walk, minus vertical".

    An external positioner swings the microphone around the speaker on ONE
    axis; it cannot raise or lower the capsule. So remote walks the longest
    prefix of :data:`CLOUD_VERIFY_POSE_PROMPTS` that asks for no
    :data:`POSITION_ROLE_XOVR` move, and any group it cannot sample is
    disclosed rather than silently missing
    (:data:`REMOTE_VERTICAL_DISCLOSURE`).

    Derived rather than written down for the same reason
    :func:`express_cloud_measure_positions` is: reordering the table must move
    this number with it instead of silently shipping a walk whose prefix now
    contains a vertical move the positioner cannot make.

    Since the 2026-08-24 geometry ruling the post-apply table is vertical-free
    BY CONSTRUCTION, so this resolves to Full's own walk and the subtraction is
    a no-op — kept, and kept derived, because "the post-apply table has no
    vertical row in it" is a property of that table rather than a promise, and
    the day someone adds one this must shorten remote's walk rather than aim a
    positioner at a pose it cannot reach.
    """
    verticals = [
        i for i, prompt in enumerate(CLOUD_VERIFY_POSE_PROMPTS)
        if prompt.role == POSITION_ROLE_XOVR
    ]
    # A group of size ``g`` walks prompts ``[:g - 1]``, so the largest
    # vertical-free group is one past the first vertical's index.
    positions = (verticals[0] + 1) if verticals else DEFAULT_CLOUD_VERIFY_POSITIONS
    if positions < MIN_CLOUD_VERIFY_POSITIONS:
        raise CrossoverV2FlowError(
            "the remote tier's vertical-free verify walk is "
            f"{positions} positions, below the validated floor of "
            f"{MIN_CLOUD_VERIFY_POSITIONS} — CLOUD_VERIFY_POSE_PROMPTS must "
            "keep both wide lateral moves ahead of any vertical one"
        )
    return min(positions, DEFAULT_CLOUD_VERIFY_POSITIONS)


# What a remote session states about the axis its positioner cannot reach.
# ONE sentence, disclosed once per session (never a block): a consumer that
# reads this group's roles finds no ``xovr`` member — the honest reading is
# "unsampled", not "flat".
#
# It used to add "the vertical spot Full measures was not sampled this time";
# that clause went on 2026-08-18 when :data:`DEFAULT_CLOUD_VERIFY_POSITIONS`
# came down to the floor and Full's walk became the same vertical-free prefix.
# The 2026-08-24 geometry ruling made that permanent rather than incidental —
# :data:`CLOUD_VERIFY_POSE_PROMPTS` has no vertical row to give up. The rest is
# unchanged and still owed: it states a fact about THIS walk.
REMOTE_VERTICAL_DISCLOSURE = (
    "Measured on the horizontal axis only — a remote positioner cannot raise "
    "or lower the microphone, so no vertical spot was sampled."
)


@dataclass(frozen=True)
class V2PlanShape:
    """The RESOLVED (tier, N, M) triple — one value, threaded everywhere.

    Before this existed, ``prepare_v2_session`` called
    :func:`build_v2_session_spec` and :func:`build_v2_cloud_index_phase_map`
    with independent defaults and passed counts to neither: two functions that
    MUST agree, agreeing only by luck. Resolving once and threading the result
    closes that desync hazard by construction — the plan the phone is handed
    and the index→phase map the session walks are derived from the same
    object or they are not built at all.
    """

    tier: str
    cloud_measure_positions: int
    cloud_verify_positions: int
    #: Whether a PERSON releases every begin — the second of the two facts the
    #: old single ``externally_positioned`` boolean carried at once. Set by the
    #: session host for a HAND-WALKED round running on the wired capture
    #: source, which has no capture page to pace it: nothing there taps, so
    #: without a hold the walk fires every capture back to back while the
    #: household is still walking to the next spot.
    #:
    #: It is not a tier. The tier still decides the (N, M) shape and whether a
    #: MACHINE advances the walk (:attr:`externally_positioned`); this says who
    #: releases each hold, which is the only thing that differs between the arm
    #: and a person holding a tape at the same bearings.
    hand_released_positions: bool = False

    def __post_init__(self) -> None:
        if self.hand_released_positions and self.externally_positioned:
            # Loud rather than idempotent: the arm's own driver releases its
            # holds, so a shape claiming BOTH movers is a caller that has not
            # decided which one is on the floor.
            raise CrossoverV2FlowError(
                f"tier {self.tier!r} is positioned by an external driver, so "
                "its holds cannot also be hand-released"
            )

    @property
    def measure_capture_target(self) -> int:
        """The cloud-INCLUSIVE shape target (``1 + N``) — CHECK plus the
        pre-apply cloud (anchor plus ``N − 1`` prompted positions), 10 at
        Full's defaults, 6 for express. NOT what stage 1 runs: the
        ``STAGE1_INCLUDES_*`` flags decide that, and the real count is
        :func:`stage1_base_entries` — two readers have been misled by the
        older wording (#2098, and the remote session-open journal). Stage 1
        applies nothing (D1), so it carries no post-apply entry at all."""
        return 1 + self.cloud_measure_positions

    @property
    def verify_capture_target(self) -> int:
        """Accepted captures STAGE 2 runs (``M``) — VERIFY's anchor plus
        ``M − 1`` prompted post-apply positions. 5 at Full
        (:data:`DEFAULT_CLOUD_VERIFY_POSITIONS`), 1 for express (whose whole
        post-apply check is the anchor at the mark).
        """
        return self.cloud_verify_positions

    @property
    def capture_target(self) -> int:
        """Accepted captures the WHOLE JOURNEY runs (``1 + N + M``).

        No single session emits this any more — since the two-stage split it is
        the sum of two sessions' targets (:attr:`measure_capture_target` and
        :attr:`verify_capture_target`), which is what the tier chooser's
        household-facing "N measurements" claim is about: the household is
        choosing both stages when it picks a tier.
        """
        return self.measure_capture_target + self.verify_capture_target

    @property
    def measure_max_attempts(self) -> int:
        """Stage 1's admission budget (its entries + geometry retakes + spare)."""
        return (
            self.measure_capture_target
            + GEOMETRY_RETRY_POSITIONS
            + CLOUD_RETAKE_ALLOWANCE
        )

    @property
    def verify_max_attempts(self) -> int:
        """Stage 2's admission budget, derived exactly like stage 1's."""
        return (
            self.verify_capture_target
            + GEOMETRY_RETRY_POSITIONS
            + CLOUD_RETAKE_ALLOWANCE
        )

    @property
    def max_attempts(self) -> int:
        """The whole journey's admission budget — the CONSERVATIVE bound.

        Kept as the sum rather than ``max(measure, verify)`` because
        :func:`cloud_plan_max_attempts` reads it as one conservative
        cross-stage figure: the sum is strictly larger than either stage's
        own budget, so a caller sized against it is sized for both. It is
        deliberately NOT what either session emits — those read
        :attr:`measure_max_attempts` / :attr:`verify_max_attempts`.
        """
        return self.capture_target + GEOMETRY_RETRY_POSITIONS + CLOUD_RETAKE_ALLOWANCE

    @property
    def has_cloud_verify_group(self) -> bool:
        """Whether this shape emits a post-apply position GROUP at all.

        ``False`` for express (``M = 1``): VERIFY's anchor is the last entry,
        so the plan's end-screen copy rides IT rather than a group tail.
        """
        return self.cloud_verify_positions > 1

    @property
    def externally_positioned(self) -> bool:
        """Whether an EXTERNAL DRIVER moves the microphone between captures — see
        :func:`tier_is_externally_positioned`, which this delegates to so the
        live conductor (which holds a tier string, not a resolved shape) and the
        plan builders answer the question from ONE definition.

        **This is the ADVANCE axis, and only that.** It decides one behaviour:
        every entry auto-begins behind the cancelable countdown
        (:func:`_entry_advance`), because there is no hand to tap. It also
        implies :attr:`positions_gated` — a countdown alone would fire into an
        arm still in motion — but the converse does not hold, and that is the
        whole reason the two are separate properties: a person can walk the same
        bearings and release each hold by hand, which needs the gate and must
        NOT get the countdown.

        The combination this refuses is auto-advance WITHOUT a gate: that is
        the bug the countdown would have replaced the tap with, and it is
        unreachable by construction because :attr:`positions_gated` reads this
        property as one of its own disjuncts.
        """
        return tier_is_externally_positioned(self.tier)

    @property
    def positions_gated(self) -> bool:
        """Whether poses are stated as BEARINGS and every begin is HELD until
        something reports the microphone in place.

        **The POSE-STATEMENT axis** — the second of the two independent facts
        the old single boolean carried. It decides, together:

        * the prompt copy restates each pose as its angle
          (:func:`_positioned_prompt`), because whoever moves the microphone is
          working in degrees rather than tape-measure centimetres; and
        * every entry declares that angle in machine terms
          (:data:`POSITION_DEG_KEY` / :data:`POSITION_ROLE_KEY`,
          :func:`_entry_policy`), which is what the session host's position gate
          reads to name the target it is waiting for.

        Those two are one statement in two vocabularies — the sentence a person
        reads and the number the gate acts on — so they share one property
        rather than drifting as two.

        True for the arm (:attr:`externally_positioned`), whose driver POSTs the
        release, and for a hand-released round
        (:attr:`hand_released_positions`), where a person does. What separates
        those two is :attr:`externally_positioned` alone: the arm also gets the
        countdown, the person keeps the tap.
        """
        return self.externally_positioned or self.hand_released_positions


def tier_is_externally_positioned(tier: Any) -> bool:
    """Whether ``tier`` names a tier an EXTERNAL DRIVER positions.

    Deliberately LENIENT where :func:`normalize_tier` is strict: this is asked
    by surfaces that hold whatever tier string a durable state file carried —
    including ``""`` (a session written before tiers existed) and words from a
    later build — and none of those may take down a group close. An unknown tier
    is simply "not externally positioned", which is the safe answer: it keeps
    the hand-walked behaviour every such session already had.
    """
    return str(tier or "").strip().lower() == TIER_REMOTE


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


# The tiers that are ONE named (N, M) pair rather than a configurable range.
# Values are callables so each pair stays DERIVED at call time from the prompt
# table it mirrors — a table edit moves the shapes, it does not strand them.
#
# Remote takes FULL's pre-apply N (its stage 1 is Full's stage 1, walked by a
# positioner instead of by hand) and its own vertical-free M.
_FIXED_SHAPE_TIERS: dict[str, Callable[[], tuple[int, int]]] = {
    TIER_EXPRESS: lambda: (
        express_cloud_measure_positions(), EXPRESS_CLOUD_VERIFY_POSITIONS,
    ),
    TIER_REMOTE: lambda: (
        remote_cloud_measure_positions(), remote_cloud_verify_positions(),
    ),
}


def resolve_plan_shape(
    tier: Any = None,
    *,
    cloud_measure_positions: int | None = None,
    cloud_verify_positions: int | None = None,
) -> V2PlanShape:
    """Resolve (and validate) one plan shape from a tier and optional counts.

    Express and remote each admit EXACTLY one (N, M) pair
    (:data:`_FIXED_SHAPE_TIERS`) — they are named shapes, not configurable
    ranges, so an explicit count that disagrees is a caller bug rather than a
    preference. Full keeps the shipped ranges
    (``MIN_CLOUD_MEASURE_POSITIONS..MAX_CLOUD_MEASURE_POSITIONS``,
    ``M >= MIN_CLOUD_VERIFY_POSITIONS``).
    """
    name = normalize_tier(tier)
    if name in _FIXED_SHAPE_TIERS:
        n, m = _FIXED_SHAPE_TIERS[name]()
        for label, wanted, got in (
            ("cloud_measure_positions", n, cloud_measure_positions),
            ("cloud_verify_positions", m, cloud_verify_positions),
        ):
            if got is not None and int(got) != wanted:
                raise CrossoverV2FlowError(
                    f"the {name} tier is a fixed shape: {label} must be "
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


def stage1_plan_max_attempts(
    capture_target: int, *, include_cloud_measure: bool,
) -> int:
    """The admission budget a stage-1 plan of ``capture_target`` entries emits.

    THE producer of that number, with two readers: ``build_v2_capture_plan``
    sets ``CapturePlan.max_attempts`` from it, and ``session_lateral_walk`` asks
    it whether a composed walk still fits ``MAX_CAPTURE_PLAN_ATTEMPTS``. A copy
    of it is a gate that refuses plans the relay would have taken.

    Geometry retakes are that cloud group's lever, so they are budgeted only
    when one is planned. Derived from the entries a plan ACTUALLY emits, never
    from the shape's cloud-only arithmetic.
    """
    return (
        capture_target
        + (GEOMETRY_RETRY_POSITIONS if include_cloud_measure else 0)
        + CLOUD_RETAKE_ALLOWANCE
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
    pre-apply prompt-table length check below, which is a property of the
    TABLE — see it for why the post-apply group's fit is checked elsewhere.
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
    # The PRE-apply group indexes :data:`CLOUD_POSITION_PROMPTS`, so that table
    # bounds N here.
    #
    # M is NOT bounded here, and the omission is deliberate. Both groups walked
    # this one table until the 2026-08-24 geometry ruling, and ``max(n, m) - 1``
    # was the honest bound then. The post-apply group now walks a table this
    # function cannot see: ``verify_prompts`` is resolved at plan-build time,
    # and a shape is resolved before it. Bounding M against the DEFAULT set
    # would refuse a caller who supplied a longer one — the very parameter the
    # ruling added — while still not checking the set that caller actually
    # walks. So the fit is checked where the table is known, in
    # :func:`build_v2_verify_capture_plan`, which refuses a shape and a pose set
    # that disagree.
    if n - 1 > len(CLOUD_POSITION_PROMPTS):
        raise CrossoverV2FlowError(
            f"the pre-apply cloud group needs {n - 1} position prompts but "
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
    separate from ``capture_protocol.MAX_CAPTURE_PLAN_ATTEMPTS`` (a SANITY
    ceiling) for the reason ``CAPTURE_PLAN_MAX_ATTEMPTS`` states: conflating
    the two is how a sanity-ceiling change silently becomes a product change.
    23 at the full tier's shipped defaults, 14 for express.
    """
    return _shape_from_kwargs(
        plan_shape,
        tier=tier,
        cloud_measure_positions=cloud_measure_positions,
        cloud_verify_positions=cloud_verify_positions,
    ).max_attempts


# One owner for "does stage 1 capture a pre-apply cloud?". R15 (#2106) says NO.
# `prepare_v2_session` runs the plan and `tier_display_info` quotes it, so the
# chooser cannot advertise a walk the session does not take (#2098's pattern).
STAGE1_INCLUDES_CLOUD_MEASURE = False

# #2291's minimum new measurement: stage 1 takes ONE summed sweep at the mark
# immediately before the household applies, so the round has a "before" to
# grade its "after" against. Its own flag beside the two above, and ON, for the
# reason the issue gives — without it every round's benefit verdict is
# ``entry_baseline_unavailable``, which is exactly the blind spot that let the
# 2026-08-10 jts3 round report success over a speaker three spec bands out.
#
# Gate 0's producer/consumer pairing is satisfied: the consumer
# (:mod:`jasper.active_speaker.crossover_v2.verification`'s benefit verdict and
# adoption table) is already merged, and the capture is what it has been
# waiting on.
#
# Applied at the PRODUCTION seams (``stage1_base_entries``,
# ``prepare_v2_session``) exactly like its two siblings — the builders below
# keep whatever a caller asks for.
STAGE1_INCLUDES_ENTRY_BASELINE = True


# R16's 6-pose lateral walk (plan §4.4) is NOT a stage-1 group. It ran from R17
# to feed the lateral robustness term of a corner selector, was paused on
# 2026-08-18 because that statistic ranked below its own noise — same-candidate repeat
# noise 3.54 dB against a 0.004-2.13 dB rank-1-to-rank-2 gap over 8 banked
# rounds, none of which it ever moved off the configured Fc — and was retired
# with the corner hunt it fed. What is NOT in doubt is the measurement: the
# poses are clean (inter-driver drift 0.6-1.9 dB against a 0.09-0.32 dB
# mark-return floor), which is why an operator's staged angle walk still runs
# them as evidence for the forward model. The pose table, the prompts, the
# per-pose screens and ladder, ``lateral_pose_curve`` and
# ``lateral_mark_return_drift_db`` all serve that walk; only the stage-1 arming
# is gone, so the two builders below still take ``include_lateral`` from
# whatever a caller asks for, exactly like ``STAGE1_INCLUDES_CLOUD_MEASURE``.
def stage1_base_entries(plan_shape: V2PlanShape | None = None) -> int:
    """Stage 1's REAL capture count, not the cloud-inclusive shape target.

    Also the ``base_entries`` a session hands a staged angle walk — the
    captures it takes that are NOT the walk (``include_lateral=False``) — so a
    price stated before Start counts the same base the session will run,
    whichever way the ``STAGE1_INCLUDES_*`` flags are set. ``None`` resolves
    the default shape, which is what a surface pricing a walk before any tier
    is chosen has.
    """
    return len(build_v2_cloud_index_phase_map(
        plan_shape=plan_shape,
        include_cloud_measure=STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=False,
        include_entry_baseline=STAGE1_INCLUDES_ENTRY_BASELINE,
    ))


# Capture-plan index → phase. APPLYING is a control-page phase (no capture)
# that sits between MEASURE-accepted and VERIFY-armed, so it has no index.
# This is the pre-cloud 3-entry layout, kept as the fallback for a session
# constructed with no explicit ``index_phase_map``; the shipped session builds
# its map through :func:`build_v2_cloud_index_phase_map` below.
#
# Frozen because it is a shared module-level default: today's one consumer,
# ``JourneyPlan.from_index_map``, copies before it freezes, so an in-place
# mutation here would corrupt every later session rather than fail loudly.
DEFAULT_INDEX_PHASE_MAP: Mapping[int, str] = MappingProxyType(
    {1: PHASE_CHECK, 2: PHASE_MEASURE, 3: PHASE_VERIFY}
)


def build_v2_cloud_index_phase_map(
    *,
    plan_shape: V2PlanShape | None = None,
    tier: Any = None,
    cloud_measure_positions: int | None = None,
    cloud_verify_positions: int | None = None,
    include_cloud_measure: bool = True,
    include_lateral: bool = False,
    include_entry_baseline: bool = False,
    lateral_prompts: Sequence[CloudPositionPrompt] | None = None,
) -> dict[int, str]:
    """Capture-plan index → session phase for a STAGE-1 (measure) session.

    The wired driver walks 1-based indexes where ``index == accepted_count +
    1``, so this map is also the running order::

        1                    CHECK
        2                    MEASURE            (design-axis anchor)
        3 .. L+2             LATERAL            (L prompted poses, R16 §4.4)
        L+3 .. L+N+1         CLOUD_MEASURE      (N-1 prompted positions)
        (last)               ENTRY_BASELINE     (#2291's "before", at the mark)

    The lateral walk runs BEFORE any pre-apply cloud because it is the anchor's
    own robustness sample: it replays the anchor program, and the sooner it runs
    after the anchor the less the household and the room have had to drift.

    The entry baseline runs LAST — after the walk and after any cloud — because
    #2291 asks for the summed capture *immediately before apply*, and every
    entry it followed would otherwise be time the room and the microphone had
    to drift between the "before" and the graph change it is meant to bracket.
    It prompts the household back to the mark, so it is one held-still capture
    rather than a group.

    **There is deliberately no VERIFY entry** (two-stage commission work order
    D1, issue #1806). Stage 1 measures and stops at the group-close confirm;
    nothing is applied inside it, so nothing post-apply can be measured by it.
    The post-apply half is stage 2's own session and its own map — see
    :func:`build_v2_verify_index_phase_map`. VERIFY's absence here is what
    ``jasper.web.correction_crossover_v2_status._phase_from_state`` reads to
    resolve a
    measure-only session to the review interlude instead of "your speaker is
    tuned".

    Single source of truth: ``build_v2_capture_plan`` builds its entries from
    this same function, so an entry's prompt can never address a different
    phase than the session believes it is running.

    ``lateral_prompts`` is the walk's own table (L is its length); ``None`` is
    the ratified one.
    """
    shape = _shape_from_kwargs(
        plan_shape,
        tier=tier,
        cloud_measure_positions=cloud_measure_positions,
        cloud_verify_positions=cloud_verify_positions,
    )
    n = shape.cloud_measure_positions
    lateral_table = LATERAL_POSE_PROMPTS if lateral_prompts is None else lateral_prompts
    mapping = {1: PHASE_CHECK, 2: PHASE_MEASURE}
    nxt = 3
    if include_lateral:
        for offset in range(len(lateral_table)):
            mapping[nxt + offset] = PHASE_LATERAL
        nxt += len(lateral_table)
    if include_cloud_measure:
        for offset in range(n - 1):
            mapping[nxt + offset] = PHASE_CLOUD_MEASURE
        nxt += n - 1
    if include_entry_baseline:
        mapping[nxt] = PHASE_ENTRY_BASELINE
    return mapping


def announced_capture_indexes(index_phase: Mapping[int, str]) -> tuple[int, ...]:
    """The 1-based captures of this plan that play the courtesy prelude.

    The consent screen tells the household what it will hear, and since the
    prelude announces a SESSION rather than a capture
    (:func:`~jasper.active_speaker.crossover_v2.programs.courtesy_prelude_for_phase`)
    "what it will hear" is no longer the same sentence for every measurement.
    Stage 1 announces its FIRST (CHECK) and its LAST (the entry baseline, which
    plays stage 2's anchor object); stage 2's walk announces its first alone.

    Derived from the SAME ``index -> phase`` map the plan's own entries are
    built from, so the sentence and the schedule cannot describe different
    sessions — the reason that map exists at all. Reading the plan's
    ``kind_label``s instead would work only because those strings happen to
    spell the phases, which is a coincidence, not a contract.
    """
    return tuple(
        index for index, phase in sorted(index_phase.items())
        if courtesy_prelude_for_phase(phase)
    )


def build_v2_verify_index_phase_map(
    *,
    plan_shape: V2PlanShape | None = None,
) -> dict[int, str]:
    """Capture-plan index → session phase for a STAGE-2 (verify) session.

    ::

        1                    VERIFY             (design-axis anchor, at the mark)
        2 .. M               CLOUD_VERIFY       (M-1 prompted positions)

    ``plan_shape is None`` is the shipped 1-entry recovery re-verify —
    ``{1: PHASE_VERIFY}``, byte-identical to what the verify-only preparer
    hardcoded before the split. A shape supplies the tier's own post-apply walk
    (work order D2, owner-confirmed 2026-07-29): express is ``M = 1`` and so
    resolves to the same single-entry map; Full is the multi-position spatial
    walk whose combined curve the after-chart, the post-apply spec verdict, and
    the delta probe all read.
    """
    m = 1 if plan_shape is None else plan_shape.verify_capture_target
    mapping = {1: PHASE_VERIFY}
    for offset in range(m - 1):
        mapping[2 + offset] = PHASE_CLOUD_VERIFY
    return mapping


# --------------------------------------------------------------------------- #
# failure taxonomy (§5.10) — owned by


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
# event).
AUTO_ADVANCE_COUNTDOWN_S = 5

# Auto-advance policy vocabulary carried in the per-entry ``screen`` field
# (page policy, not a protocol change — the field is opaque to the schema).
AUTO_ADVANCE_TAP = "tap"            # requires the user's tap (first capture)
AUTO_ADVANCE_COUNTDOWN = "countdown"  # auto-begins behind a cancelable countdown
# Armed by the apply-complete host event. RETAINED but emitted by no plan
# since PR-T3 (D10): stage 1 has no VERIFY entry and stage 2 opens
# already-applied, so nothing waits on an apply inside a session any more. Kept
# as plan-grammar vocabulary — the page and the runner both still understand it
# — and deliberately not deleted; no new design may depend on it.
AUTO_ADVANCE_ON_APPLY = "on_apply"

# PROVISIONAL (W6.10 fold-in): phone-inactivity budget for the very FIRST begin
# of a v2 session (before any capture). The microphone-check screen's placement
# instructions alone legitimately take longer than the general 120 s
# ``DEFAULT_TIMEOUT_S`` to read — Chrome round 1 collapsed here — so the v2 runner
# widens only this first window. Every later window keeps the tight per-phase
# arm/upload backstop; re-derive from W6 bench observation.
V2_FIRST_BEGIN_TIMEOUT_S = 300.0


def v2_first_begin_timeout_s() -> float:
    """The first-begin budget in force — the constant above, env-overridable.

    Four commissioning sessions on the 2026-08-16 walk died at exactly the 300 s
    default in ``phase=awaiting_begin``. Widening it is a ``jasper.env`` edit
    (``JASPER_V2_FIRST_BEGIN_TIMEOUT_S``) rather than a rebuild; out-of-range or
    unparseable values fall back to the default, mirroring the
    ``JASPER_CAPTURE_ALIGNMENT_THRESHOLD`` pattern.

    The ceiling is DERIVED from ``capture_protocol.MAX_TTL_S`` rather than
    written here: nothing outliving that sanity ceiling can be honoured,
    whatever this knob says, and a second copy of that bound would be free
    to drift from it.
    """

    from jasper.capture_protocol import MAX_TTL_S

    return bounded_env_float(
        "JASPER_V2_FIRST_BEGIN_TIMEOUT_S", V2_FIRST_BEGIN_TIMEOUT_S,
        lo=30.0, hi=float(MAX_TTL_S),
    )


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


def _positioned_prompt(
    prompt: CloudPositionPrompt, shape: V2PlanShape | None,
) -> CloudPositionPrompt:
    """One pose's prompt, in the vocabulary the shape's OPERATOR acts on.

    A tap-paced shape keeps the tape-measure copy verbatim (byte-identical); a
    GATED one (:attr:`V2PlanShape.positions_gated` — the arm, or a person
    releasing each hold by hand) restates the SAME pose as its angle, because
    that is the number the gate publishes and the operator is asked for. Only
    the sentence differs — ``offset_cm`` and ``role`` are untouched, so the
    durable evidence a gated session records stays comparable with a
    tape-measured one's.
    """
    if shape is not None and shape.positions_gated:
        return remote_position_prompt(prompt)
    return prompt


def _entry_advance(shape: V2PlanShape | None) -> dict[str, str]:
    """The §5.2 auto-advance fields one plan entry carries, from its SHAPE.

    Hand-advanced shapes get :data:`AUTO_ADVANCE_TAP` and no countdown key —
    BYTE-IDENTICAL to what every entry emitted when the policy was a literal at
    each site, which is what ``_GOLDEN_V2_PLAN_BYTES`` pins. That includes a
    hand-RELEASED shape (:attr:`V2PlanShape.hand_released_positions`): its
    begins are gated, but a person is there to tap, so a countdown would fire
    while they are still walking. An externally positioned shape
    (:attr:`V2PlanShape.externally_positioned`) gets the countdown instead,
    because no hand is there to tap; its per-entry begin is then held by the
    position gate until the driver reports the angle reached, so the countdown
    only ever runs out into a capture the gate has released.

    ``shape is None`` is the recovery re-verify, which has no tier and keeps the
    tap.

    ``countdown_s`` is a STRING because ``CapturePlanEntry.screen`` is a
    ``str -> str`` map on the wire; the page does ``Number(screen.countdown_s)``.
    """
    if shape is not None and shape.externally_positioned:
        return {
            "auto_advance": AUTO_ADVANCE_COUNTDOWN,
            "countdown_s": str(AUTO_ADVANCE_COUNTDOWN_S),
        }
    return {"auto_advance": AUTO_ADVANCE_TAP}


#: The per-entry screen keys that state a GATED entry's TARGET POSITION in
#: machine terms. ``screen`` is an opaque ``str -> str`` bag the page ignores
#: unknown keys in (the same seam ``auto_advance`` / ``noise_note`` /
#: ``confirm_title`` already ride), so this is not a protocol change.
#:
#: Emitted ONLY by a GATED shape (:attr:`V2PlanShape.positions_gated`). They
#: exist because the
#: position gate has to name the angle it is waiting for, and the alternative —
#: re-deriving it from the entry's index against a second copy of the plan's
#: index arithmetic — is two sources of truth for one number. The PLAN is the
#: source; the gate and the envelope read it back off the entry the runner
#: already hands them.
POSITION_DEG_KEY = "position_deg"
POSITION_ROLE_KEY = "position_role"


def _entry_policy(
    shape: V2PlanShape | None, prompt: CloudPositionPrompt | None = None,
) -> dict[str, str]:
    """One entry's non-copy ``screen`` fields: advance policy + target position.

    ``prompt is None`` means an entry with no prompted pose of its own — CHECK,
    MEASURE, the entry baseline, and stage 2's anchor — every one of which is a
    0° design-axis capture, so that is what it declares. Gating those too is
    deliberate: a gated session's operator has to put the microphone back on the
    axis for them exactly as they moved it away, and a gate that trusted "it is
    probably still there" would measure whatever the last pose left behind.

    The two halves answer different questions and so read different facts: the
    advance policy is the MOVER's (:func:`_entry_advance`), the target position
    is the GATE's (:attr:`V2PlanShape.positions_gated`).
    """
    policy = _entry_advance(shape)
    if shape is None or not shape.positions_gated:
        return policy
    degrees = position_angle_deg(prompt) if prompt is not None else 0
    role = prompt.role if prompt is not None else POSITION_ROLE_ONAX
    return {
        **policy,
        POSITION_DEG_KEY: str(degrees),
        POSITION_ROLE_KEY: role,
    }


def _cloud_entry_screen(
    *, progress: str, title: str, body: str, policy: Mapping[str, str],
) -> dict[str, str]:
    return {
        "progress": progress,
        "title": title,
        "body": body,
        **policy,
    }


def build_v2_capture_plan(
    roles_bands: Sequence[RoleBand],
    fc_hz: float | None,
    *,
    plan_shape: V2PlanShape | None = None,
    tier: Any = None,
    cloud_measure_positions: int | None = None,
    cloud_verify_positions: int | None = None,
    include_cloud_measure: bool = True,
    include_lateral: bool = False,
    include_entry_baseline: bool = False,
    lateral_prompts: Sequence[CloudPositionPrompt] | None = None,
) -> Any:
    """The STAGE-1 (measure) CapturePlan (§5.7 + flat-linearization PR-3b).

    CHECK and MEASURE are required; the pre-apply cloud, the lateral walk, and
    #2291's entry baseline are optional. The layout
    ``build_v2_cloud_index_phase_map`` documents, built from that same function
    so prompt and phase cannot disagree.

    When included, the pre-apply cloud ends stage 1 and holds for an explicit
    completion signal. R15 omits that cloud, so its CHECK/MEASURE plan closes
    normally. Both shapes leave Apply to the untimed jts.local review interlude;
    post-apply capture remains stage 2's own session.

    **Screen grammar (flow-simplification §2.1).** Every entry's ``screen``
    carries ``progress`` (the one server-derived counter), ``title`` (ONE
    imperative instruction) and ``body`` (at most one supporting clause).
    Screens are an opaque ``str -> str`` map, so none of this is a
    relay/protocol change.

    Entry durations derive from the composed programs (MEASURE sized from a
    nominal gain plan — sweep/gap lengths are gain-independent, so the duration
    is exact even before CHECK's solve) plus a lead/tail margin; each entry's
    ``screen`` carries the phase prompt AND the §5.2 auto-advance policy:
    CHECK and MEASURE each require a tap (MEASURE is the longest and loudest
    capture of the session — issue #1823; see the entry below), and every
    prompted cloud position requires the operator's tap — the mic has to be
    MOVED between them, so a countdown would fire into a hand still in flight.

    No phone-side mechanism is new: ``CapturePlanEntry.screen`` and
    ``AUTO_ADVANCE_TAP`` already carry per-entry copy the page renders and gates
    on, and the deployed page reads ``max_attempts``/``capture_target``
    generically with no plan-length cap of its own.
    """
    from jasper.capture_protocol import CapturePlan, CapturePlanEntry

    roles = tuple(roles_bands)
    # Every program below asks ``courtesy_prelude_for_phase`` for its OWN phase
    # (issue #1677): this is the phone's DURATION BUDGET, so it must agree with
    # what the session actually plays — ``crossover_v2.programs``'s
    # ``SessionExcitation`` composers, which ask the same function — or the
    # phone stops recording before the real (prelude-lengthened) program ends.
    check = build_check_program(
        roles, courtesy_prelude=courtesy_prelude_for_phase(PHASE_CHECK),
    )
    nominal_gains = {rb.role: BASE_STIMULUS_PEAK_DBFS for rb in roles}
    # MEASURE's own entry and every lateral pose, which replays it verbatim.
    measure = build_measure_program(
        nominal_gains, roles,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=courtesy_prelude_for_phase(PHASE_MEASURE),
    )
    # The entry baseline plays the VERIFY-shaped summed sweep, so its DURATION
    # is the verify program's even though stage 1 runs no VERIFY phase of its
    # own — and it is the ANNOUNCED one, because its program object is stage 2's
    # anchor (``program_for_phase``'s compared pair).
    # The summed sweep's band for a speaker with no corner — the same value
    # ``SessionExcitation`` composes the played program at.
    band_hz = measurement_band_hz(roles)
    verify = build_verify_program(
        fc_hz,
        measurement_band_hz=band_hz,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=courtesy_prelude_for_phase(PHASE_ENTRY_BASELINE),
    )
    # A prompted position's twin of it: same sweep, no prelude.
    cloud = build_verify_program(
        fc_hz,
        measurement_band_hz=band_hz,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=courtesy_prelude_for_phase(PHASE_CLOUD_MEASURE),
    )
    shape = _shape_from_kwargs(
        plan_shape,
        tier=tier,
        cloud_measure_positions=cloud_measure_positions,
        cloud_verify_positions=cloud_verify_positions,
    )
    index_phase = build_v2_cloud_index_phase_map(
        plan_shape=shape,
        include_cloud_measure=include_cloud_measure,
        include_lateral=include_lateral,
        include_entry_baseline=include_entry_baseline,
        lateral_prompts=lateral_prompts,
    )
    target = len(index_phase)
    verify_ms = _program_duration_ms(verify) + CAPTURE_ENTRY_MARGIN_MS
    cloud_ms = _program_duration_ms(cloud) + CAPTURE_ENTRY_MARGIN_MS
    measure_ms = _program_duration_ms(measure) + CAPTURE_ENTRY_MARGIN_MS
    # One policy for every entry of this plan, from the shape (§5.2). The FIRST
    # entry's value is inert either way — the page starts round 1 from the
    # spec's own begin button, and only ever reads the policy of the entry AFTER
    # an accepted capture — so a uniform value is both simplest and safe.
    advance = _entry_policy(shape)
    entries: list[Any] = [
        CapturePlanEntry(
            index=0,
            kind_label="check",
            duration_ms=_program_duration_ms(check) + CAPTURE_ENTRY_MARGIN_MS,
            screen={
                "progress": capture_progress_label(1, target),
                "title": (
                    "Stand the microphone about 1 m in front of the speaker, "
                    "at tweeter height."
                ),
                # NOT "stay quiet" (work order D8, issue #1835). CHECK's 12 s
                # ambient window is the SESSION's room-noise measurement and it
                # is deliberately composed to run BEFORE anyone is asked to
                # hush (jasper.audio_measurement.program's module docstring);
                # the gain solve reads it, so a pre-hushed room reads quieter
                # than reality and the solve under-drives against the noise the
                # later sweeps actually face. The measurement-honest request is
                # to carry on. The speaker asks for quiet itself, on the
                # in-sweep windows where quiet genuinely is wanted — a
                # different window with a different purpose, which this must
                # not collapse into one sentence.
                "body": (
                    "JTS listens to the room exactly as it is first, so carry "
                    "on as you were — it will say when to be quiet."
                ),
                # The phone's OWN pre-arm floor window, which is a third
                # measurement again: a sub-second reading of what the
                # microphone hears, taken before the speaker plays anything.
                # It gets its own sentence for the same reason — asking for
                # quiet there hushes the room a moment before CHECK measures
                # it. Absent on every other entry, where the page's default
                # (quiet, because a sweep follows immediately) stays right.
                "noise_note": (
                    "Listening to the room as it normally is — carry on as "
                    "you were."
                ),
                **advance,
            },
        ),
        CapturePlanEntry(
            index=1,
            kind_label="measure",
            duration_ms=measure_ms,
            screen={
                "progress": capture_progress_label(2, target),
                "title": "Keep the microphone still — this spot is the mark.",
                # MEASURE is the session's LONGEST capture and the one that
                # CAN be its loudest — since #1825/#1829 each driver's level is
                # solved to the SNR the fit needs in its own band, so a quiet
                # room gets a quiet MEASURE and a noisy one still gets the full
                # level. Until issue #1823 it rolled straight out of CHECK on a
                # countdown:
                # the household went from one capture into a much louder one
                # with no chance to say "not yet". Same-spot auto-advance was
                # the right instinct — no movement is needed — but it read as
                # the speaker taking a liberty. One tap, with copy that says
                # what is coming, buys the consent back. That ruling is
                # about a HOUSEHOLD's consent, so it binds the hand-walked
                # tiers only: an externally positioned shape has no hand to
                # take a liberty with, and its entries auto-advance through
                # the same countdown vocabulary (AUTO_ADVANCE_COUNTDOWN,
                # AUTO_ADVANCE_COUNTDOWN_S, and the page's
                # renderPlanCountdown) behind the position gate. The remote
                # tier is that path's first shipped consumer — the comment
                # beside renderPlanCountdown in capture-page/js/main.js still
                # says nothing reaches it, and is stale as of this tier; it is
                # left for the next deliberate page publish rather than
                # forcing a republish (and a cache-invalidation stamp bump)
                # for a comment.
                #
                # Household language, not ours (coordinator ruling, 2026-07-28):
                # the tail says what the level is FOR, not which internal stage
                # asked for it. "The fit" is a word for this file, not for
                # someone holding a phone.
                "body": (
                    "This one is longer, and can be the loudest — it measures "
                    "each driver alone at the level it needs to hear each one "
                    "clearly."
                ),
                **advance,
            },
        ),
    ]
    # R16's lateral walk (plan §4.4). Same 0-based index arithmetic as the cloud
    # loop below; ``duration_ms`` is the MEASURE program's because each pose
    # replays it verbatim (``program_for_phase``), not the summed sweep's.
    lateral_indexes = [
        i for i, p in sorted(index_phase.items()) if p == PHASE_LATERAL
    ]
    lateral_table = LATERAL_POSE_PROMPTS if lateral_prompts is None else lateral_prompts
    for offset, capture_index in enumerate(lateral_indexes):
        prompt = _positioned_prompt(lateral_table[offset], shape)
        entries.append(
            CapturePlanEntry(
                index=capture_index - 1,
                kind_label="lateral",
                duration_ms=measure_ms,
                screen=_cloud_entry_screen(
                    progress=capture_progress_label(capture_index, target),
                    title=prompt.headline,
                    body=prompt.detail,
                    policy=_entry_policy(shape, prompt),
                ),
            )
        )
    # The two prompted groups. ``index_phase`` is 1-based (the relay's own
    # index space); ``CapturePlanEntry.index`` is 0-based, hence the -1.
    cloud_measure_indexes = [
        i for i, p in sorted(index_phase.items()) if p == PHASE_CLOUD_MEASURE
    ]
    for offset, capture_index in enumerate(cloud_measure_indexes):
        prompt = _positioned_prompt(CLOUD_POSITION_PROMPTS[offset], shape)
        entries.append(
            CapturePlanEntry(
                index=capture_index - 1,
                kind_label="cloud_measure",
                duration_ms=cloud_ms,
                screen=_cloud_entry_screen(
                    progress=capture_progress_label(capture_index, target),
                    title=prompt.headline,
                    body=prompt.detail,
                    policy=_entry_policy(shape, prompt),
                ),
            )
        )
    # #2291's "before" measurement, LAST. Its duration is the summed sweep's
    # (``verify_ms``) because it replays the VERIFY program verbatim — the
    # identity ``program_for_phase`` guarantees and the benefit comparison
    # depends on. A tap, like every other entry — and it stays a tap through the
    # lateral pause. The original reason was that the household had just walked
    # the poses and had to come back to the mark first; with the walk off they
    # arrive here straight from MEASURE, already at the mark, so what a tap now
    # buys is the same thing every other entry gets it for: nothing starts
    # recording until a person says they are ready.
    for capture_index in [
        i for i, p in sorted(index_phase.items()) if p == PHASE_ENTRY_BASELINE
    ]:
        entries.append(
            CapturePlanEntry(
                index=capture_index - 1,
                kind_label="entry_baseline",
                duration_ms=verify_ms,
                screen=_cloud_entry_screen(
                    progress=capture_progress_label(capture_index, target),
                    title=(
                        "Back to the design axis (0°) — one last measurement "
                        "before tuning."
                        if shape.positions_gated
                        else "Back to the mark — one last measurement before "
                        "tuning."
                    ),
                    # Says WHAT it buys, in the household's terms: this is the
                    # recording the speaker is compared against afterwards, so
                    # "it got better" becomes something measured rather than
                    # asserted. No internal vocabulary (no "baseline", no
                    # "summed sweep") — the same register as the MEASURE entry
                    # above.
                    body=(
                        "This records how the speaker sounds right now, so JTS "
                        "can tell you whether the tuning actually improved it."
                    ),
                    policy=advance,
                ),
            )
        )
    return CapturePlan(
        capture_target=target,
        max_attempts=stage1_plan_max_attempts(
            target, include_cloud_measure=include_cloud_measure,
        ),
        schema_version=2,
        entries=tuple(entries),
    )


def verify_pose_table(
    verify_prompts: Sequence[CloudPositionPrompt] | None = None,
) -> tuple[CloudPositionPrompt, ...]:
    """The post-apply walk's pose set — the caller's, or the runbook default.

    ONE resolver, so the plan the phone is handed, the index→phase map it walks,
    and the session's own :meth:`CrossoverV2Session._cloud_prompt` cannot end up
    reading three different tables. The same shape ``lateral_prompts`` already
    has for the R16 walk, and the same rule: ``None`` is
    :data:`CLOUD_VERIFY_POSE_PROMPTS`, the ratified set, and anything else is a
    caller who has decided to walk somewhere else and owns that choice.

    **The set is a parameter because the runbook is a suggestion.** The
    post-apply pose set used to be a fixed prefix of the pre-apply table, so
    "measure the result at these angles" was not a question anyone could ask —
    the 2026-08 new-horn campaign wanted the design axis in it and had to run a
    separate MEASURE round to get one. A caller states the set it wants; the
    default states what a household gets when it states nothing.
    """
    return (
        CLOUD_VERIFY_POSE_PROMPTS if verify_prompts is None
        else tuple(verify_prompts)
    )


def build_v2_verify_capture_plan(
    fc_hz: float | None,
    *,
    measurement_band_hz: tuple[float, float] | None = None,
    plan_shape: V2PlanShape | None = None,
    verify_prompts: Sequence[CloudPositionPrompt] | None = None,
) -> Any:
    """The post-apply (STAGE 2) plan — the tier's own verify walk, or the
    1-entry recovery re-arm.

    ``plan_shape is None`` is the shipped §5.2 recovery re-verify, byte-
    identical to what it has always been: one entry, ``CAPTURE_PLAN_MAX_
    ATTEMPTS``, and copy that LEADS with how cheap it is (§2.4 — the 2026-07-27
    hardware session abandoned this recovery because nothing on screen said
    "Try again" was one sweep rather than another walk). It is what a FAILED
    stage 2 offers, and what ``/crossover/v2/verify`` still does by default.

    A ``plan_shape`` builds **stage 2 of the two-stage commission flow** (work
    order D2, owner-confirmed 2026-07-29): ``M`` entries — VERIFY's design-axis
    anchor at the mark plus ``M − 1`` prompted post-apply positions — so each
    tier keeps its shipped verify shape. Express is ``M = 1`` (its whole
    post-apply check is the anchor, and it makes no cross-position claim);
    Full is the multi-position spatial walk whose combined curve the
    after-chart, the post-apply spec verdict, and the delta probe all read.
    Running Full's
    stage 2 as a single position would leave its post-apply group with 0 curves
    and no combine at all, and would make ``_TIER_CLAIMS``' "re-check the
    result at several spots around the mark" untrue.

    **The §2.2 confirm-then-tone tap survives, re-anchored to stage 2's own
    begin** (work order D10). The anchor entry carries ``confirm_title`` /
    ``confirm_body`` verbatim, so the tone still waits for the household to
    say they are standing on the mark — what changed is only the ordering
    premise (there is no in-session apply to confirm *after* any more).

    ``verify_prompts`` is the pose set this walk takes; ``None`` is the
    ratified one (:func:`verify_pose_table`). The shape's ``M`` and the table's
    length must agree — a shape asking for more prompted poses than the table
    supplies is refused here rather than walked short.
    """
    from jasper.capture_protocol import CapturePlan, CapturePlanEntry

    # The anchor is stage 2's OPENING capture, so it is announced; the prompted
    # positions behind it are not (``courtesy_prelude_for_phase``). Two nominal
    # programs because the phone budgets each entry from the program that entry
    # will actually record.
    verify = build_verify_program(
        fc_hz,
        measurement_band_hz=measurement_band_hz,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=courtesy_prelude_for_phase(PHASE_VERIFY),
    )
    cloud = build_verify_program(
        fc_hz,
        measurement_band_hz=measurement_band_hz,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=courtesy_prelude_for_phase(PHASE_CLOUD_VERIFY),
    )
    verify_ms = _program_duration_ms(verify) + CAPTURE_ENTRY_MARGIN_MS
    cloud_ms = _program_duration_ms(cloud) + CAPTURE_ENTRY_MARGIN_MS
    if plan_shape is None:
        entry = CapturePlanEntry(
            index=0,
            kind_label="verify",
            duration_ms=verify_ms,
            screen={
                "progress": capture_progress_label(1, 1),
                "title": REVERIFY_NO_REWALK_HEADLINE,
                "body": "Put the microphone back on the mark and hold it still.",
                # No shape, so no tier: the recovery re-arm keeps the tap.
                **_entry_advance(None),
            },
        )
        return CapturePlan(
            capture_target=1,
            max_attempts=CAPTURE_PLAN_MAX_ATTEMPTS,
            schema_version=2,
            entries=(entry,),
        )
    index_phase = build_v2_verify_index_phase_map(plan_shape=plan_shape)
    target = plan_shape.verify_capture_target
    # The phone's END screen once every capture completes
    # (capture-page/js/main.js's renderPlanAllDone reads the FINAL wire index's
    # entry) — owner ruling, 2026-07-20: state the outcome plainly and point at
    # the speaker page for undo/compare, rather than the shared "All
    # measurements done" generic copy. This is STAGE-2 copy and moved here with
    # stage 2: it is the end of the journey, not the end of a measurement.
    #
    # WHICH entry is last depends on the tier: Full ends on the post-apply
    # group's tail, express (M = 1) ends on the anchor itself. Express also
    # says LESS, because it verified less — it confirmed the result at the mark
    # and made no cross-position post-apply claim at all (§1.3).
    #
    # **Every word here is written BEFORE THE FIRST TONE PLAYS** — this whole
    # plan is built when stage 2 is armed — so nothing on this screen may
    # assert a MEASURED outcome (issue #1964). Each tier's copy states only
    # what arming already establishes: the correction is applied (stage 2 only
    # exists because the household applied it), and reaching this screen at all
    # means the tracking comparator PASSED (a tracking fail returns
    # ``PhaseVerdict(False, …)``, so the phone renders a retry, never this).
    #
    # Full used to say "Verified and applied." That is the post-apply cloud's
    # SPEC verdict, which is computed after the last capture and can fail while
    # tracking passes — the household then read "Verified and applied" on the
    # phone and "Your speaker is tuned, BUT the result still measures further
    # from flat than the target…" on jts.local, for one session. The phone
    # cannot carry that caveat: its component vocabulary
    # (``sweep_spec.UI_COMPONENT_TYPES``) has no result-shaped member,
    # and the only runtime seam that could deliver one — the relay's
    # LAST-WRITE-WINS host-event slot — is routinely overwritten by
    # ``capture_set_complete`` before the phone's ~250 ms poll reads the final
    # ``capture_result``. So the verdict has ONE owner, jts.local's done
    # screen, and this screen says where it is rather than guessing it.
    #
    # **Express's upgrade-path phrase is COPIED, not authored** (B2 fix,
    # adversarial review of PR #1780). It read "for the verified-everywhere
    # result", which that review ruled an overclaim — a Full walk re-checks a
    # handful of prompted spots around the mark, never every point in the room
    # — and the corrected phrase already ships on jts.local in two places
    # (``crossover_envelope_v2``'s express ``done_verdict`` and
    # ``_TIER_CLAIMS[TIER_FULL]``). The phone kept the withdrawn wording, so
    # one journey said both things: the same cross-surface divergence the
    # paragraph above closes. Spell it the way the wizard spells it, so a
    # future re-wording has one place to start rather than three.
    done_screen = {
        "done_title": "Your speaker is tuned",
        "done_body": (
            "The speaker page has the result — manage or undo there."
            if plan_shape.has_cloud_verify_group
            else "Confirmed at the mark and applied. Run a Full measurement "
            "for the result checked at several spots around the mark, or "
            "manage this one on the speaker page."
        ),
    }
    advance = _entry_policy(plan_shape)
    # The two facts asked separately, because this screen reads both: the COPY
    # follows the pose statement (a gated operator is given the bearing), the
    # confirm tap below follows the advance policy (only a machine-advanced
    # session has no hand to answer it).
    positions_gated = plan_shape is not None and plan_shape.positions_gated
    externally_positioned = (
        plan_shape is not None and plan_shape.externally_positioned
    )
    anchor_screen: dict[str, str] = {
        "progress": capture_progress_label(1, target),
        "title": (
            "Back on the design axis (0°) — one sweep to check the result."
            if positions_gated
            else "Back at the mark — one sweep to check the result."
        ),
        "body": (
            f"{MARK_DISTANCE_M:g} m out, pointed at the speaker."
            if positions_gated
            else "Same spot, same height, pointed at the speaker."
        ),
        **advance,
    }
    if not externally_positioned:
        # §2.2's confirm-then-tone tap, on stage 2's own begin (D10). Same two
        # strings the single-session plan carried, so the grammar the household
        # learned in stage 1 is the grammar stage 2 opens with.
        #
        # OMITTED for an externally positioned shape, and the omission is
        # load-bearing rather than cosmetic: ``entryConfirmsBeforeArming``
        # (capture-page/js/main.js) treats a present ``confirm_title`` as "hold
        # the tone until somebody taps", so carrying it into an unattended
        # session would park the anchor on a confirm screen with no hand to
        # answer it and burn the runner's ``awaiting_arm`` budget. The promise
        # the tap makes — the microphone is where the plan says before the tone
        # plays — is the POSITION GATE's promise here, made by the driver that
        # actually moved it.
        #
        # A hand-RELEASED shape keeps the confirm, which is why this arm reads
        # the advance policy rather than ``positions_gated``: there IS a hand
        # there, and the strings are byte-identical to what every tap-paced
        # shape has always emitted.
        anchor_screen.update({
            "confirm_title": "Back on the mark, holding still?",
            "confirm_body": "Same spot, same height, pointed at the speaker.",
        })
    if not plan_shape.has_cloud_verify_group:
        anchor_screen.update(done_screen)
    entries: list[Any] = [
        CapturePlanEntry(
            index=0,
            kind_label="verify",
            duration_ms=verify_ms,
            screen=anchor_screen,
        )
    ]
    cloud_verify_indexes = [
        i for i, p in sorted(index_phase.items()) if p == PHASE_CLOUD_VERIFY
    ]
    table = verify_pose_table(verify_prompts)
    # EQUALITY, not "the table is long enough". Both sides are real:
    #
    #   * a table SHORTER than the walk prompts fewer spots than the session
    #     believes it is running, and
    #   * a table LONGER is silently walked as a PREFIX — which is worse,
    #     because it is quiet. The poses past ``M - 1`` never reach an entry,
    #     while :func:`build_v2_verify_session_spec` quotes the orientation's
    #     reach off the WHOLE resolved table: a 5-pose walk carrying a 60 cm
    #     sixth pose reaches 50 cm and promises 70 cm on the wire. A household
    #     told how much room to clear is owed the walk's own number.
    #
    # Gated on :attr:`V2PlanShape.has_cloud_verify_group` because express is
    # ``M = 1``: it emits NO cloud-verify entry at all, so a bare ``!=`` would
    # measure its empty index list against the 5-row default and refuse the one
    # shipped shape that is correct by construction.
    if plan_shape.has_cloud_verify_group and len(cloud_verify_indexes) != len(table):
        raise CrossoverV2FlowError(
            f"a post-apply group of {target} positions walks "
            f"{len(cloud_verify_indexes)} prompted poses but the pose set "
            f"supplies {len(table)} — give the shape the M its table earns "
            "(1 + len(poses))"
        )
    for offset, capture_index in enumerate(cloud_verify_indexes):
        prompt = _positioned_prompt(table[offset], plan_shape)
        screen = _cloud_entry_screen(
            progress=capture_progress_label(capture_index, target),
            title=prompt.headline,
            body=prompt.detail,
            policy=_entry_policy(plan_shape, prompt),
        )
        if offset == len(cloud_verify_indexes) - 1:
            screen.update(done_screen)
        entries.append(
            CapturePlanEntry(
                index=capture_index - 1,
                kind_label="cloud_verify",
                duration_ms=cloud_ms,
                screen=screen,
            )
        )
    return CapturePlan(
        capture_target=target,
        max_attempts=plan_shape.verify_max_attempts,
        schema_version=2,
        entries=tuple(entries),
    )


def build_v2_verify_session_spec(
    fc_hz: float | None,
    *,
    measurement_band_hz: tuple[float, float] | None = None,
    acknowledgement_binding: str,
    plan_shape: V2PlanShape | None = None,
    verify_prompts: Sequence[CloudPositionPrompt] | None = None,
    **spec_kwargs: Any,
) -> Any:
    """The capture spec for a post-apply session (stage 2, or §5.2 recovery).

    **The consent surface is chosen by the PLAN's own shape, not by the caller's
    intent**, so a one-sweep session and a walk can never advertise each other's
    copy. A single-capture plan — the recovery re-verify, and Express's stage 2,
    which really is one held-still sweep at the mark — keeps the stationary
    consent copy and LEADS with :data:`REVERIFY_NO_REWALK_HEADLINE`, because the
    2026-07-27 hardware session abandoned this recovery for want of that
    sentence (§2.4) and a household who has just walked a cloud needs it just
    as much. A multi-capture plan (Full's stage 2) is a walk, so it takes the
    guided consent surface with its own capture count and tier, exactly as
    :func:`build_v2_session_spec` does for stage 1.
    """
    # Resolved ONCE here and handed to both readers below, so the sentence the
    # orientation quotes and the entries the walk prompts are literally the same
    # object rather than two calls that happen to agree.
    verify_table = verify_pose_table(verify_prompts)
    plan = build_v2_verify_capture_plan(
        fc_hz, measurement_band_hz=measurement_band_hz,
        plan_shape=plan_shape, verify_prompts=verify_table,
    )
    walked = plan.capture_target > 1
    extra: dict[str, Any] = (
        {
            "guided_captures": plan.capture_target,
            "guided_tier": plan_shape.tier if plan_shape is not None else "",
            # Stage 2's walk is oriented on the same terms as stage 1's (work
            # order D7): a post-apply cloud discovered one prompt at a time is
            # the same defect. Quoted off the SAME resolved pose set the entries
            # above were built from, so the sentence cannot describe a reach the
            # walk does not have.
            "walk_shape": cloud_walk_shape(verify_table, post_apply=True),
            # Stage 2 announces its anchor and nothing behind it — same
            # derivation as stage 1's, off the same index -> phase map.
            "announced_captures": announced_capture_indexes(
                build_v2_verify_index_phase_map(plan_shape=plan_shape)
            ),
        }
        if walked
        else {"reverify_lead": REVERIFY_NO_REWALK_HEADLINE}
    )
    return build_crossover_sweep_spec(
        driver_label="crossover verification",
        driver_role="summed",
        acknowledgement_binding=acknowledgement_binding,
        stimulus_duration_ms=max(entry.duration_ms for entry in plan.entries),
        capture_plan=plan,
        **extra,
        **spec_kwargs,
    )


def wall_clock_ceiling_s(capture_target: int) -> float:
    """The ceiling for a plan of ``capture_target`` captures.

    The arithmetic :func:`session_wall_clock_ceiling_s` states, reachable by a
    caller holding a capture count rather than a plan object.
    """
    from jasper.active_speaker.session_volume_plan import (
        DEFAULT_WALL_CLOCK_CEILING_S,
        MAX_WALL_CLOCK_CEILING_S,
    )

    extra = max(0, capture_target - CAPTURE_PLAN_TARGET)
    return min(
        MAX_WALL_CLOCK_CEILING_S,
        DEFAULT_WALL_CLOCK_CEILING_S + extra * WALL_CLOCK_CEILING_PER_ENTRY_S,
    )


def session_wall_clock_ceiling_s(capture_plan: Any) -> float:
    """The walked-away volume ceiling for one plan, scaled by its length.

    ``session_volume_plan.DEFAULT_WALL_CLOCK_CEILING_S`` (1800 s) was sized
    for the 3-entry flow. A 15-capture commission is a genuinely
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

    Since the two-stage split (work order D2) each STAGE arms its own ceiling
    from its own plan, and this function is unchanged — it reads whatever plan
    it is handed. The per-stage entry counts are
    :func:`tier_display_info`'s ``stage1_captures`` / ``stage2_captures``, and
    the ceiling for any one of those plans is this function applied to it —
    derive them there rather than restating numbers here, where a plan change
    cannot reach them. (An earlier revision of this docstring attributed
    ``build_v2_capture_plan()``'s BARE-DEFAULTS scenario — 10 entries, 2640 s —
    to the shipped Full tier's stage 1, which is a different and smaller plan.
    Two valid scenarios; only one of them is what ships.) At the 19-entry
    maximum the unclamped value would be 3720 s and the plan's hard cap binds at
    3600 s.
    """
    target = int(getattr(capture_plan, "capture_target", CAPTURE_PLAN_TARGET) or 0)
    return wall_clock_ceiling_s(target)


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
#
# NOT derived from the household's declared driver low limit (#2603), and that
# is deliberate rather than an oversight. A declaration DOES exist by the time
# this screen renders, but the only resolution path for it
# (``resolve_conductor_context``) is refuse-if-not-ready and can regenerate the
# crossover preview file as a SIDE EFFECT -- unacceptable for a value this
# module recomputes on every ~1.5 s poll. Reading it into this memoized,
# argument-less function instead would go stale the moment the operator edits
# the declaration, which is a worse failure than a fixed representative pair.
# So this stays a display-only fallback with its own safe-bias rationale, and
# a per-poll side-effect-free read is tracked as separate cosmetic work.
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
    each tier displays the SAME whole minutes in every case checked. The figure
    itself is this function's output; do not write it down elsewhere, and note
    that the test pins the sweep against ``tier_display_info()`` itself, so it
    cannot catch a docstring that quotes a stale figure — an earlier revision of
    this one did exactly that. The invariant is empirical rather than
    structural: the binding margin is the TIGHTEST per-stage headroom before the
    next minute boundary (the two stages ceil separately), and across that sweep
    it is a fraction of the 60 s quantum — which is what would need re-deriving
    if a future change genuinely widened the plausible band space. The
    two-stage split moved the displayed figures by its own arithmetic rather
    than by a longer session: the journey is TWO plans and each ceils to a whole
    minute separately, which is the deliberately conservative choice recorded
    below. ``capture_target`` needs no audio program at all — it is pure
    arithmetic on the resolved :class:`V2PlanShape`.

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
                "capture_target": (stage1_base_entries(shape)
                                   + shape.verify_capture_target),
                "estimated_minutes": 0,
                # Present even here: the chooser's copy reads both, and a
                # KeyError on the degraded path would take the whole
                # microphone_check screen down over a duration it already
                # knows how to render as unknown.
                "stage1_captures": stage1_base_entries(shape),
                "stage2_captures": shape.verify_capture_target,
            }
            for tier, shape in ((t, resolve_plan_shape(t)) for t in TIERS)
        }


@lru_cache(maxsize=1)
def _tier_display_info_cached() -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for tier in TIERS:
        shape = resolve_plan_shape(tier)
        # BOTH stages (two-stage commission D2). ``capture_target`` has always
        # been the whole journey's count, and after the split the duration has
        # to be the whole journey's too or the chooser quotes the Full tier's
        # whole-journey count against stage 1's minutes alone. Two ceils rather
        # than one is deliberately conservative: this is a DISPLAY number and the
        # household really does pay two per-session set-ups.
        #
        # T4 owns the stage-aware WORDING this derivation makes possible ("N
        # now, M after you apply"); this is the arithmetic underneath it.
        stage1 = build_v2_capture_plan(
            _DISPLAY_ROLES_BANDS, _DISPLAY_FC_HZ, plan_shape=shape,
            include_cloud_measure=STAGE1_INCLUDES_CLOUD_MEASURE,
            include_lateral=False,
            include_entry_baseline=STAGE1_INCLUDES_ENTRY_BASELINE,
        )
        stage2 = build_v2_verify_capture_plan(_DISPLAY_FC_HZ, plan_shape=shape)
        out[tier] = {
            # Summed from the plans: the shape's own `capture_target` counts a
            # pre-apply cloud stage 1 no longer walks.
            "capture_target": stage1.capture_target + shape.verify_capture_target,
            "estimated_minutes": (
                stage1.estimated_minutes() + stage2.estimated_minutes()
            ),
            # The per-stage split T4's chooser copy states, off the SHAPE's own
            # two targets — the same properties the two plan builders size
            # themselves from, so the chooser and the plans cannot quote
            # different sessions, and their sum is ``capture_target`` by
            # construction. Available without an audio program, which is what
            # lets the fallback path below answer with the same numbers rather
            # than omitting the keys the chooser copy reads.
            "stage1_captures": stage1.capture_target,
            "stage2_captures": shape.verify_capture_target,
        }
    return out


def build_v2_session_spec(
    roles_bands: Sequence[RoleBand],
    fc_hz: float | None,
    *,
    acknowledgement_binding: str,
    plan_shape: V2PlanShape | None = None,
    tier: Any = None,
    cloud_measure_positions: int | None = None,
    cloud_verify_positions: int | None = None,
    include_cloud_measure: bool = True,
    include_lateral: bool = False,
    include_entry_baseline: bool = False,
    lateral_prompts: Sequence[CloudPositionPrompt] | None = None,
    **spec_kwargs: Any,
) -> Any:
    """One stage-1 capture spec, optionally including the pre-apply cloud (§5.7).

    Rides :func:`~.sweep_spec.build_crossover_sweep_spec` (same kind and
    placement-acknowledgement machinery) with its stage-1 plan attached, and
    selects guided consent only when that plan includes the pre-apply cloud —
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
    shape = _shape_from_kwargs(
        plan_shape,
        tier=tier,
        cloud_measure_positions=cloud_measure_positions,
        cloud_verify_positions=cloud_verify_positions,
    )
    plan = build_v2_capture_plan(
        roles_bands, fc_hz, plan_shape=shape,
        include_cloud_measure=include_cloud_measure,
        include_lateral=include_lateral,
        include_entry_baseline=include_entry_baseline,
        lateral_prompts=lateral_prompts,
    )
    longest_ms = max(entry.duration_ms for entry in plan.entries)
    # R16: EITHER group makes this a walk. The consent copy below was gated on
    # the cloud alone; leaving it there would promise a stationary microphone
    # to a household about to be prompted through five moves — the exact
    # dishonesty the guided wording exists to prevent.
    #
    # #2291's entry baseline is deliberately NOT a third term. A walk is a
    # session that prompts the household to MOVE the microphone, and the entry
    # baseline is one capture at the mark they are already standing at — it
    # asks them to come back, not to go anywhere new. ``walk_shape_for`` agrees
    # by construction: with neither group on it computes a reach of 0 cm and
    # returns "", so an entry-baseline-only session that claimed ``walked``
    # would emit guided consent copy with no shape line under it — a promise of
    # a walk with nothing to describe.
    walked = include_cloud_measure or include_lateral
    return build_crossover_sweep_spec(
        driver_label="crossover",
        driver_role="summed",
        acknowledgement_binding=acknowledgement_binding,
        stimulus_duration_ms=longest_ms,
        capture_plan=plan,
        # The consent surface must describe the walk, not a stationary mic —
        # the count is every capture the household is prompted through, which
        # is the plan's own target.
        guided_captures=plan.capture_target if walked else 0,
        # …and WHICH of those announce themselves, so the consent screen
        # states what this session plays rather than a shape that was true
        # when every capture announced.
        announced_captures=(
            announced_capture_indexes(
                build_v2_cloud_index_phase_map(
                    plan_shape=shape,
                    include_cloud_measure=include_cloud_measure,
                    include_lateral=include_lateral,
                    include_entry_baseline=include_entry_baseline,
                    lateral_prompts=lateral_prompts,
                )
            )
            if walked
            else ()
        ),
        # …and which INSTRUMENT that walk is, so the announcement screen can
        # say "quick tune" vs "full measurement" without the spec builder
        # re-deriving a shape it does not own (§1.4 / §2.3).
        guided_tier=shape.tier if walked else "",
        # …and how far the walk reaches, in one line (work order D7's intent,
        # #1941 R1's presentation). Derived HERE, from the same table and the
        # same group size the per-entry screens above are built from, so the
        # orientation and the prompts cannot describe different walks. The
        # reach is the FURTHEST of whichever groups run, so a lateral-only
        # session quotes 40 cm rather than a cloud's ceiling it never walks.
        walk_shape=walk_shape_for(
            cloud_positions=(
                shape.cloud_measure_positions if include_cloud_measure else 0
            ),
            lateral=include_lateral,
            lateral_prompts=lateral_prompts,
        ),
        **spec_kwargs,
    )
