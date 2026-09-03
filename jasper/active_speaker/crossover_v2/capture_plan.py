# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The walk a session will do, decided before anything plays: where the
microphone goes, in what order, with what words on the screen, how many attempts
each pose is allowed, and which excitation program each capture index runs.

It decides; it does not act — no I/O, no session state, no fader, no graph. The
one side effect is a journal line on the tier-display cache. Mover-agnostic
(MS-17): positions are degrees and centimetres, and nothing here knows whether a
human or an arm moves the microphone.
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

# Total admission attempts a v2 session may spend across its entries, retakes
# included. A POLICY choice about retries, deliberately not the SANITY ceiling
# `capture_protocol.MAX_CAPTURE_PLAN_ATTEMPTS`.
CAPTURE_PLAN_MAX_ATTEMPTS = 8


# --------------------------------------------------------------------------- #
# position-group choreography
# --------------------------------------------------------------------------- #
#
# docs/historical/linearization-campaign-2026-07.md fundamental 1: N≈8-12 gated
# sweeps at guided positions, ≥10 cm spread for HF null decorrelation and
# ≥~30 cm spread to support the LF edge.

# Owned by :mod:`.contracts`.
DEFAULT_CLOUD_MEASURE_POSITIONS = _contracts.DEFAULT_CLOUD_MEASURE_POSITIONS
# Configurable floor. ``CLOUD_POSITION_PROMPTS``' wide-offset guarantee is
# specified against exactly this number.
MIN_CLOUD_MEASURE_POSITIONS = 6
# Configurable ceiling, sized so the worst-case plan still fits under
# `capture_protocol.MAX_CAPTURE_PLAN_ATTEMPTS`; `CapturePlan.max_attempts`
# validation (sweep_spec.py) enforces that fit at build time. There is no slack
# at the walk-armed bound: at N=11, M=6 the attempt sum lands on
# ``MAX_CAPTURE_PLAN_ATTEMPTS`` exactly. Raising N costs a step of configuration
# headroom or a household-visible retake (``CLOUD_RETAKE_ALLOWANCE``).
MAX_CLOUD_MEASURE_POSITIONS = 11
# Total MIC POSITIONS in the post-apply cloud, VERIFY's anchor included, so the
# plan emits ``M − 1`` prompted positions after VERIFY and the group combines
# ``M − 1`` curves (VERIFY's own summed capture answers the tracking verdict,
# not "is the speaker flat"). Pinned equal to
# ``1 + len(CLOUD_VERIFY_POSE_PROMPTS)`` by an import-time guard beside that
# table.
DEFAULT_CLOUD_VERIFY_POSITIONS = 6
# Configurable floor for the POST-apply group: below it the group carries no
# ~30 cm-class spread and voids fundamental 1's LF-edge guarantee. DERIVED from
# :data:`CLOUD_VERIFY_POSE_PROMPTS` by ``_min_positions_for_two_wide_offsets``,
# never a literal, so reordering the prompts moves the floor with them.
MIN_CLOUD_VERIFY_POSITIONS = 6

# Retake headroom a cloud plan carries above its entry count and its geometry
# retries: the same ABSOLUTE spare the 3-entry flow has, not the same ratio —
# longer sets get proportionally fewer retakes each, deliberately.
CLOUD_RETAKE_ALLOWANCE = CAPTURE_PLAN_MAX_ATTEMPTS - CAPTURE_PLAN_TARGET


# The offset class that carries fundamental 1's LF edge: at or past this
# distance a move is "wide". :attr:`CloudPositionPrompt.wide` is computed from
# this constant rather than hand-set per row, so narrowing a wide prompt's
# distance moves the derived group floors with it.
WIDE_OFFSET_MIN_CM = 30.0
# The shortest prompted move that still decorrelates HF nulls.
MIN_CLOUD_OFFSET_CM = 10.0
# How far the geometry-locked retake rungs ask the operator to go: past every
# position in the table (widest 60 cm), and no further, because a desk-scale
# setup has to be able to reach it (#1874).
GEOMETRY_RETRY_OFFSET_CM = 75.0

# Owned by :mod:`.spatial`.
POSITION_ROLE_ONAX = _spatial.POSITION_ROLE_ONAX
POSITION_ROLE_OFFAX = _spatial.POSITION_ROLE_OFFAX
POSITION_ROLE_XOVR = _spatial.POSITION_ROLE_XOVR
POSITION_ROLES = _spatial.POSITION_ROLES


def format_position_distance(offset_cm: float) -> str:
    """One prompted distance, in inches with the metric value beside it.

    Both units ride every prompt, and centimetres rather than metres because
    every prompted move is between 0.1 m and 0.6 m (#1805).
    """
    inches = round(float(offset_cm) / (METERS_PER_INCH * 100.0))
    return f"{inches:g} in ({float(offset_cm):g} cm)"


@dataclass(frozen=True)
class CloudPositionPrompt:
    """One prompted mic move in a position group.

    ``detail`` may be empty; ``text`` re-joins headline and detail for the
    durable evidence sidecar. ``offset_cm`` is the pose's SIDEWAYS displacement
    in the mark's own plane — the perpendicular leg of a right triangle whose
    other leg is :data:`MARK_DISTANCE_M` — so it says where a pose is relative
    to the design axis and nothing about how the microphone got there. ``wide``
    is computed from it, so the ~30 cm-class guarantee cannot be voided by
    editing copy alone. ``role`` names the question the position answers
    (:data:`POSITION_ROLES`).
    """

    headline: str
    detail: str = ""
    offset_cm: float = 0.0
    role: str = POSITION_ROLE_ONAX
    #: Which side of the design axis a LATERAL row sits on: ``-1`` LEFT,
    #: ``+1`` RIGHT, ``0`` for an at-mark or vertical row. Set by :func:`_pose`
    #: from the row's own ``side`` bearing, never by hand.
    lateral_sign: int = 0
    #: Which side of mark HEIGHT a row sits on: ``-1`` BELOW, ``+1`` ABOVE,
    #: ``0`` for a row that asks for no raise or lower. Set by :func:`_pose`
    #: from the row's own ``updown`` bearing.
    vertical_sign: int = 0
    #: How far above (or below) mark height the row asks for, in centimetres.
    #: Separate from ``offset_cm`` because a compound row moves two different
    #: distances at once (the second geometry-retake rung goes 75 cm sideways
    #: AND 30 cm up). ``0`` means the row asks for no raise.
    vertical_offset_cm: float = 0.0

    @property
    def wide(self) -> bool:
        """Whether this move carries the plan's ~30 cm-class LF-edge offset."""
        return float(self.offset_cm) >= WIDE_OFFSET_MIN_CM

    @property
    def at_mark(self) -> bool:
        """Whether the pose asks for no move at all — on EITHER axis."""
        return float(self.offset_cm) == 0.0 and float(self.vertical_offset_cm) == 0.0

    @property
    def text(self) -> str:
        """Headline + detail as one string — the evidence sidecar's ``prompt``.

        The sidecar is the only durable statement of where a curve was
        measured, so it records the complete instruction.
        """
        return f"{self.headline} {self.detail}".strip() if self.detail else self.headline


# Horizontal bearing convention: negative is LEFT of the design axis, positive
# is RIGHT, as seen from the microphone looking at the speaker — the viewpoint
# the prompt copy is written from.
_LATERAL_SIGNS = {"LEFT": -1, "RIGHT": 1}

# Elevation convention: negative is BELOW mark height, positive is ABOVE. The
# words are the ``updown`` slot ``_VERTICAL_POSE`` fills.
_VERTICAL_SIGNS = {"BELOW": -1, "ABOVE": 1}

# Inverted, for copy generated FROM a sign rather than parsed into one.
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
    :func:`format_position_distance`, so a row's stated distance and its
    ``offset_cm`` cannot drift apart. Refuses at import time below
    :data:`MIN_CLOUD_OFFSET_CM`, with ``ValueError`` rather than
    :class:`CrossoverV2FlowError` because the table is built while this module
    is still executing and that class is not defined yet.
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
        # Every row names exactly one direction word, so its single
        # ``offset_cm`` is the one displacement it moved and the other axis
        # keeps the neutral 0.
        lateral_sign=_LATERAL_SIGNS.get(str(bearing.get("side") or ""), 0),
        vertical_sign=vertical_sign,
        vertical_offset_cm=offset_cm if vertical_sign else 0.0,
    )


# Every wide row's supporting clause: stepping in as you go out keeps the path
# length about equal to the mark's, the precondition any later position-pair
# level comparison needs.
_WIDE_LATERAL_DETAIL = (
    "Step a little toward the speaker as you go out, so you stay about as far "
    "from it as the mark is, and keep the microphone pointed at it."
)
_VERTICAL_DETAIL = "Keep the microphone pointed at the speaker."

# The prompt table, in the order a group walks it.
#
# Every row is an ABSOLUTE pose measured from the mark, never a delta on the
# previous one, and the actor is the microphone rather than any one device
# (#1806). Copy stays hardware-blind: nothing that assumes a particular cabinet.
#
# ONE ordered table serves both groups — the pre-apply group takes ``[:N - 1]``
# and the post-apply group ``[:M - 1]`` — so the first two wide moves sit at
# offsets 3 and 4 (1-based) rather than at the end, where a shorter group would
# never reach them. Reordering this table moves two derived numbers
# (``MIN_CLOUD_VERIFY_POSITIONS`` and ``express_cloud_measure_positions()``).
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

# --- lateral evidence ------------------------------------------------------- #
#
# The lateral walk reuses the table above's ±12 cm and ±40 cm left/right moves,
# selected by PREDICATE rather than slice index so reordering that table cannot
# silently swap which poses the walk asks for.
_LATERAL_POSE_OFFSETS_CM = (12.0, 40.0)

# The walk opens and closes at the mark. Both rows bypass ``_pose`` because a
# 0 cm move cannot clear :data:`MIN_CLOUD_OFFSET_CM`, and they exist to
# CORRELATE with each other rather than to decorrelate. The anchor MEASURE at
# the same spot is not a substitute: its evidence is composed to the configured
# crossover when analyzed, while a pose is kept neutral.
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

# The four SIDE poses both angle walks are made of, derived from the cloud table
# by predicate (see ``_LATERAL_POSE_OFFSETS_CM``).
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

# The derivation above must yield exactly one LEFT and one RIGHT at each
# declared offset, bracketed by the two at-mark poses: a lopsided walk's
# left/right disagreement term is meaningless.
if len(LATERAL_POSE_PROMPTS) != 2 * len(_LATERAL_POSE_OFFSETS_CM) + 2:
    raise ValueError(
        "the lateral walk must derive exactly one LEFT and one RIGHT pose at "
        f"each of {_LATERAL_POSE_OFFSETS_CM} cm, bracketed by the two at-mark "
        f"poses, got {len(LATERAL_POSE_PROMPTS)} poses"
    )

# --- the POST-APPLY walk's own pose set -------------------------------------- #
#
# The design axis is a MEMBER of this walk, not just the anchor in front of it
# (owner ruling, 2026-08-24): VERIFY's anchor is consumed by the tracking
# verdict and never joins the group, so without this row the post-apply group
# banks no on-axis position record at all.
#
# Derived from the same ``_SIDE_POSE_PROMPTS`` the lateral walk uses, so an edit
# to the shared offsets moves both walks together, and vertical-free BY
# CONSTRUCTION, which is what lets ``remote_cloud_verify_positions`` stop
# clamping. The at-mark row bypasses ``_pose`` because a 0 cm move cannot clear
# :data:`MIN_CLOUD_OFFSET_CM`; that floor is a property of the group, and the
# four sides beside this row carry the whole ±7/±22 spread the combine needs.
VERIFY_MARK_PROMPT = CloudPositionPrompt(
    headline="Stay on the mark — one sweep from here first.",
    detail="Same spot, same height, pointed at the speaker.",
    offset_cm=0.0,
    role=POSITION_ROLE_ONAX,
)

CLOUD_VERIFY_POSE_PROMPTS: tuple[CloudPositionPrompt, ...] = (
    (VERIFY_MARK_PROMPT,) + _SIDE_POSE_PROMPTS
)

# The shipped post-apply group is the anchor plus this table, so a table edit
# that did not move ``DEFAULT_CLOUD_VERIFY_POSITIONS`` with it would silently
# walk a prefix.
if DEFAULT_CLOUD_VERIFY_POSITIONS != 1 + len(CLOUD_VERIFY_POSE_PROMPTS):
    raise ValueError(
        "the post-apply group is VERIFY's anchor plus every pose in "
        f"CLOUD_VERIFY_POSE_PROMPTS, so DEFAULT_CLOUD_VERIFY_POSITIONS must be "
        f"{1 + len(CLOUD_VERIFY_POSE_PROMPTS)}, not "
        f"{DEFAULT_CLOUD_VERIFY_POSITIONS}"
    )

# --- remote tier: the same walk, stated as ANGLES (external positioner) ------ #
#
# Owned by :mod:`.spatial`.
MARK_DISTANCE_M = _spatial.MARK_DISTANCE_M


def position_angle_deg(prompt: CloudPositionPrompt) -> int:
    """The signed horizontal bearing of one lateral pose, in WHOLE degrees.

    ``atan(offset / mark distance)`` in the mark's own plane, signed by
    :data:`_LATERAL_SIGNS`, so ``-7`` is 7° LEFT of the design axis; the shipped
    offsets give ±7° (12 cm) and ±22° (40 cm). Whole degrees because the offsets
    are tape-measure distances to a mark placed "about" 1 m out.

    #2932 is open: this is a TANGENT construction, which puts the capsule at
    ``mark / cos(θ)`` rather than a constant radius — treat the bearing as sound
    and the equidistance claim as unverified.

    Refuses a :data:`POSITION_ROLE_XOVR` row rather than returning ``0``, which
    would aim a positioner at the mark while the plan believed it had sampled
    the crossover axis.
    """
    if prompt.role == POSITION_ROLE_XOVR:
        raise CrossoverV2FlowError(
            "a vertical position has no horizontal bearing: an external "
            "positioner cannot raise or lower the microphone, so an "
            f"externally positioned walk must contain no {POSITION_ROLE_XOVR} "
            "pose (see remote_cloud_verify_positions)"
        )
    if float(prompt.offset_cm) != 0.0 and prompt.lateral_sign == 0:
        # An off-axis pose that declared no side would multiply out to 0° —
        # "already on the design axis" — and bank an offset the microphone
        # never had.
        raise CrossoverV2FlowError(
            f"a lateral position {float(prompt.offset_cm):g} cm off the mark "
            "declares no side, so it has no signed bearing — build it through "
            "_pose (or set lateral_sign) rather than letting it read as 0°"
        )
    radians = math.atan2(float(prompt.offset_cm) / 100.0, MARK_DISTANCE_M)
    return int(round(prompt.lateral_sign * math.degrees(radians)))


def position_elevation_deg(prompt: CloudPositionPrompt) -> int:
    """The signed ELEVATION of one pose above mark height, in WHOLE degrees.

    ``atan(vertical_offset_cm / MARK_DISTANCE_M)``, over the row's own
    ``vertical_offset_cm`` rather than ``offset_cm`` because a compound row
    moves both ways at once. Refuses nothing: a row asking for no raise signs
    ``0``, which is true of it — an unstated elevation has an honest zero where
    an unstated bearing does not.
    """
    if prompt.vertical_sign == 0:
        return 0
    radians = math.atan2(float(prompt.vertical_offset_cm) / 100.0, MARK_DISTANCE_M)
    return int(round(prompt.vertical_sign * math.degrees(radians)))


def position_geometry(prompt: CloudPositionPrompt) -> _spatial.PositionGeometry:
    """One pose's WHERE, as the four fields its retained record carries.

    TOTAL where :func:`position_angle_deg` refuses, because this runs on the
    retention path and a derivation that raised would fail a capture the
    household already gave: each refusal becomes a recorded ``degrees=None``,
    never a ``0`` that would read as "on the design axis".
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


#: Owned by :mod:`.spatial`.
_DESIGN_AXIS_GEOMETRY = _spatial._DESIGN_AXIS_GEOMETRY


def remote_position_prompt(prompt: CloudPositionPrompt) -> CloudPositionPrompt:
    """One hand-walked pose, restated as the ANGLE a positioner turns to.

    Same pose, same ``offset_cm``, same role — only the copy changes, so
    everything downstream reads exactly what Full's walk records.
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

# The apply hold's screen body. It carries a REPOSITION instruction because the
# pre-apply cloud ends at a wide offset while VERIFY's tracking comparator is
# only meaningful back on the design axis.
VERIFY_ANCHOR_HOLD_MESSAGE = (
    "Applying the measured crossover to your speaker. While that finishes, put "
    "the microphone back on the mark — same spot, same height, pointed at the "
    "speaker."
)

# The sentence the 1-entry re-verify re-arm leads with, on BOTH of its surfaces
# (the consent screen's steps and the plan entry's own instruction), so the two
# cannot drift apart.
REVERIFY_NO_REWALK_HEADLINE = (
    "One sweep, back at the mark — you do NOT need to redo the walk."
)

# What the geometry-locked retake asks for. Two rungs, so a second retake is a
# genuinely different instruction. Same register as the position table (#1805):
# numeric distances in both units, absolute poses measured from the mark.
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
#: machine-readable half of the sentences above, since these rungs are built
#: outside :func:`_pose`. Read into
#: :attr:`CloudPositionPrompt.vertical_offset_cm` by ``_prompt_shown_for``.
CLOUD_GEOMETRY_RETRY_RISE_CM: tuple[float, ...] = (0.0, WIDE_OFFSET_MIN_CM)

# A rung added without a rise beside it would bank mark height for whatever it
# asks for.
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

    A group of size ``g`` walks offsets ``[:g - 1]``, so the answer is one past
    the index of the second wide prompt. ``prompts`` defaults to the PRE-apply
    :data:`CLOUD_POSITION_PROMPTS`; the post-apply group passes its own table.
    """
    table = CLOUD_POSITION_PROMPTS if prompts is None else tuple(prompts)
    wide = [i for i, prompt in enumerate(table) if prompt.wide]
    if len(wide) < 2:
        raise CrossoverV2FlowError(
            "a cloud walk's table must supply at least two wide offsets — "
            "fundamental 1's LF edge needs ~30 cm-class spread"
        )
    return wide[1] + 2


# Here rather than beside :data:`CLOUD_VERIFY_POSE_PROMPTS` only because the
# derivation it checks is defined immediately above.
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


# What happens AFTER the walk, in one clause. Deliberately promises no tune in
# either case: whether anything is applied is the household's call.
CLOUD_WALK_SHAPE_TAIL = "Afterwards you decide what to do about what JTS heard."
CLOUD_WALK_SHAPE_TAIL_POST_APPLY = (
    "Afterwards the speaker page shows how the tune did."
)

# The granularity the orientation's REACH is quoted at. Rounded STRICTLY up so
# the quoted number is a true ceiling: the wide rows' equidistance step-in puts
# the capsule on a chord, so a stated 40 cm move really lands ~40.9 cm from the
# mark at the placement copy's nominal 1 m. The geometry retake (75 cm, ~80.8 cm
# on rung 2) is deliberately NOT absorbed and gets its own clause instead.
CLOUD_WALK_REACH_ROUNDING_CM = 10.0


def cloud_walk_reach_cm(positions: int) -> float:
    """The ceiling the orientation quotes for a walk of ``positions`` captures.

    The ``[:positions - 1]`` slice of :data:`CLOUD_POSITION_PROMPTS`, rounded
    strictly up to the next :data:`CLOUD_WALK_REACH_ROUNDING_CM`.
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

    Rung 2 is a COMPOUND pose (:data:`GEOMETRY_RETRY_OFFSET_CM` sideways *and*
    :data:`WIDE_OFFSET_MIN_CM` up), so its displacement is the hypotenuse.
    """
    return max(
        GEOMETRY_RETRY_OFFSET_CM,
        math.hypot(GEOMETRY_RETRY_OFFSET_CM, WIDE_OFFSET_MIN_CM),
    )


def cloud_walk_shape(
    prompts: Sequence[CloudPositionPrompt], *, post_apply: bool = False,
) -> str:
    """The walk's SHAPE in one sentence, for the pre-session orientation screen.

    ``prompts`` is the resolved table the caller handed its plan builder, not a
    count it might slice differently, so the orientation cannot describe a reach
    the walk does not have. ``post_apply`` selects stage 2's tail.
    """
    return _walk_shape(cloud_walk_reach_cm_of(prompts), post_apply=post_apply)


def walk_shape_for(
    *, cloud_positions: int, lateral: bool,
    lateral_prompts: Sequence[CloudPositionPrompt] | None = None,
) -> str:
    """The orientation sentence for a stage-1 session's ACTUAL groups.

    One sentence quoting the furthest reach of whichever groups run.
    ``lateral_prompts`` is the walk this session actually takes; ``None`` is the
    ratified table, which a taken angle walk can reach far past.
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
        # A group with no prompted moves is not a walk and gets no shape line.
        return ""
    tail = (
        CLOUD_WALK_SHAPE_TAIL_POST_APPLY if post_apply
        else CLOUD_WALK_SHAPE_TAIL
    )
    # Conditional on the retake actually reaching past the quoted ceiling, so a
    # narrowed retake drops the clause rather than keeping a stale one.
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
# commission tiers
# --------------------------------------------------------------------------- #

# The named plan SHAPES a session can be opened with. A tier is a distinct,
# validated (N, M) pair, not a loosened floor, so ``MIN_CLOUD_MEASURE_POSITIONS``
# never moves to accommodate express.
TIER_FULL = "full"
TIER_EXPRESS = "express"
# The EXTERNALLY DRIVEN tier (experimental): Full's stage-1 shape walked by a
# mic positioner over HTTP, so it is the one tier whose entries auto-begin
# (:data:`AUTO_ADVANCE_COUNTDOWN`) behind a per-entry POSITION GATE and the one
# whose positions are stated as ANGLES. Not a household choice — it is in
# :data:`TIERS` only so ``normalize_tier`` admits a POST naming it.
TIER_REMOTE = "remote"
TIERS = (TIER_FULL, TIER_EXPRESS, TIER_REMOTE)
DEFAULT_TIER = TIER_FULL

# Express's post-apply group: VERIFY's design-axis anchor and nothing else. An
# ``M = 1`` plan emits NO cloud-verify entries, so express makes no
# cross-position post-apply claim — it verifies tracking at the mark
# (``VERIFY_TOLERANCE_DB``) and says so.
EXPRESS_CLOUD_VERIFY_POSITIONS = 1


def express_cloud_measure_positions() -> int:
    """Express's pre-apply group size — the shortest prompted cloud that still
    contains BOTH of :data:`CLOUD_POSITION_PROMPTS`' wide (~30 cm-class) moves.
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

    Safe only because :data:`STAGE1_INCLUDES_CLOUD_MEASURE` is ``False``, so the
    ``[:N - 1]`` prefix of :data:`CLOUD_POSITION_PROMPTS` — which at Full's N
    contains vertical rows an external positioner cannot reach — is never
    walked. The guard below refuses at the point those two facts stop agreeing.
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
    """Remote's post-apply group size — Full's walk, minus vertical.

    An external positioner swings on ONE axis and cannot raise or lower the
    capsule, so remote walks the longest prefix of
    :data:`CLOUD_VERIFY_POSE_PROMPTS` asking for no
    :data:`POSITION_ROLE_XOVR` move; what it cannot sample is disclosed
    (:data:`REMOTE_VERTICAL_DISCLOSURE`). The post-apply table is vertical-free
    today, so the subtraction is a no-op — kept derived so adding a vertical row
    shortens remote's walk rather than aiming a positioner at it.
    """
    verticals = [
        i for i, prompt in enumerate(CLOUD_VERIFY_POSE_PROMPTS)
        if prompt.role == POSITION_ROLE_XOVR
    ]
    # A group of size ``g`` walks prompts ``[:g - 1]``.
    positions = (verticals[0] + 1) if verticals else DEFAULT_CLOUD_VERIFY_POSITIONS
    if positions < MIN_CLOUD_VERIFY_POSITIONS:
        raise CrossoverV2FlowError(
            "the remote tier's vertical-free verify walk is "
            f"{positions} positions, below the validated floor of "
            f"{MIN_CLOUD_VERIFY_POSITIONS} — CLOUD_VERIFY_POSE_PROMPTS must "
            "keep both wide lateral moves ahead of any vertical one"
        )
    return min(positions, DEFAULT_CLOUD_VERIFY_POSITIONS)


# What a remote session states about the axis its positioner cannot reach. ONE
# sentence, disclosed once per session: a consumer reading this group's roles
# finds no ``xovr`` member, and the honest reading is "unsampled", not "flat".
REMOTE_VERTICAL_DISCLOSURE = (
    "Measured on the horizontal axis only — a remote positioner cannot raise "
    "or lower the microphone, so no vertical spot was sampled."
)


@dataclass(frozen=True)
class V2PlanShape:
    """The RESOLVED (tier, N, M) triple — one value, threaded everywhere.

    The plan the phone is handed and the index→phase map the session walks must
    derive from one of these or they can disagree.
    """

    tier: str
    cloud_measure_positions: int
    cloud_verify_positions: int
    #: Whether a PERSON releases every begin. Set for a HAND-WALKED round on the
    #: wired capture source, which has no capture page to pace it: without a
    #: hold the walk fires every capture back to back while the household is
    #: still walking. Not a tier — the tier decides the (N, M) shape and whether
    #: a MACHINE advances the walk (:attr:`externally_positioned`).
    hand_released_positions: bool = False

    def __post_init__(self) -> None:
        if self.hand_released_positions and self.externally_positioned:
            # The arm's own driver releases its holds, so a shape claiming
            # BOTH movers has not decided which one is on the floor.
            raise CrossoverV2FlowError(
                f"tier {self.tier!r} is positioned by an external driver, so "
                "its holds cannot also be hand-released"
            )

    @property
    def measure_capture_target(self) -> int:
        """The cloud-INCLUSIVE shape target (``1 + N``) — CHECK plus the
        pre-apply cloud. NOT what stage 1 runs: the ``STAGE1_INCLUDES_*`` flags
        decide that, and the real count is :func:`stage1_base_entries`."""
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

        The sum rather than ``max(measure, verify)``, so a guard that passes on
        it passes on both stages. Not what either session emits — those read
        :attr:`measure_max_attempts` / :attr:`verify_max_attempts`.
        """
        return self.capture_target + GEOMETRY_RETRY_POSITIONS + CLOUD_RETAKE_ALLOWANCE

    @property
    def has_cloud_verify_group(self) -> bool:
        """Whether this shape emits a post-apply position GROUP at all.

        ``False`` for express (``M = 1``), whose end-screen copy rides VERIFY's
        anchor rather than a group tail.
        """
        return self.cloud_verify_positions > 1

    @property
    def externally_positioned(self) -> bool:
        """Whether an EXTERNAL DRIVER moves the microphone between captures.

        The ADVANCE axis, and only that: every entry auto-begins behind the
        cancelable countdown (:func:`_entry_advance`). Implies
        :attr:`positions_gated` — a countdown alone would fire into an arm still
        in motion — but not the converse: a person can walk the same bearings
        and release each hold by hand, which needs the gate and not the
        countdown.
        """
        return tier_is_externally_positioned(self.tier)

    @property
    def positions_gated(self) -> bool:
        """Whether poses are stated as BEARINGS and every begin is HELD until
        something reports the microphone in place.

        The POSE-STATEMENT axis: the prompt copy restates each pose as its angle
        (:func:`_positioned_prompt`) and every entry declares that angle in
        machine terms (:data:`POSITION_DEG_KEY` / :data:`POSITION_ROLE_KEY`,
        :func:`_entry_policy`), which is what the host's position gate reads.
        True for the arm and for a hand-released round; only the arm also gets
        the countdown.
        """
        return self.externally_positioned or self.hand_released_positions


def tier_is_externally_positioned(tier: Any) -> bool:
    """Whether ``tier`` names a tier an EXTERNAL DRIVER positions.

    Deliberately LENIENT where :func:`normalize_tier` is strict: callers hold
    whatever tier string a durable state file carried, and an unknown one is
    "not externally positioned" rather than a refusal that takes down a close.
    """
    return str(tier or "").strip().lower() == TIER_REMOTE


def normalize_tier(tier: Any) -> str:
    """Allowlist a household-supplied tier id; empty/absent means FULL.

    Strict about the value, lenient about absence: an unset tier keeps the full
    instrument, an unknown one asks for an instrument this build does not have.
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
# table it mirrors.
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
    (:data:`_FIXED_SHAPE_TIERS`), so an explicit count that disagrees is
    refused. Full keeps the shipped ranges.
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
        # Routed through the shared table-length check so a shortened prompt
        # table fails here rather than at entry-build time.
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

    Passing both is refused rather than silently preferring one.
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

    Geometry retakes are the cloud group's lever, so they are budgeted only when
    one is planned. Derived from the entries a plan actually emits, never from
    the shape's cloud-only arithmetic.
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

    The FULL tier keeps the shipped ranges; express is exempt from them because
    :func:`resolve_plan_shape` has already pinned its N and M to the derived
    constants. Both share the pre-apply prompt-table length check below.
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
    # bounds N here. M is deliberately NOT bounded: the post-apply group walks a
    # table resolved at plan-build time, so its fit is checked where that table
    # is known, in :func:`build_v2_verify_capture_plan`.
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
    plus ``M − 1``).
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
    ceiling) for the reason ``CAPTURE_PLAN_MAX_ATTEMPTS`` states.
    """
    return _shape_from_kwargs(
        plan_shape,
        tier=tier,
        cloud_measure_positions=cloud_measure_positions,
        cloud_verify_positions=cloud_verify_positions,
    ).max_attempts


# One owner for "does stage 1 capture a pre-apply cloud?" (#2106). Applied at
# the production seams so the chooser cannot advertise a walk the session does
# not take; the builders below keep whatever a caller asks for.
STAGE1_INCLUDES_CLOUD_MEASURE = False

# #2291: stage 1 takes ONE summed sweep at the mark immediately before the
# household applies, so the round has a "before" to grade its "after" against.
# Without it every round's benefit verdict is ``entry_baseline_unavailable``.
STAGE1_INCLUDES_ENTRY_BASELINE = True


# The lateral walk is NOT a stage-1 group: only its stage-1 arming is gone, so
# the builders below still take ``include_lateral`` from whatever a caller asks
# for. An operator's staged angle walk still runs the poses as evidence for the
# forward model.
def stage1_base_entries(plan_shape: V2PlanShape | None = None) -> int:
    """Stage 1's REAL capture count, not the cloud-inclusive shape target.

    Also the ``base_entries`` a session hands a staged angle walk — the captures
    it takes that are NOT the walk (``include_lateral=False``). ``None``
    resolves the default shape.
    """
    return len(build_v2_cloud_index_phase_map(
        plan_shape=plan_shape,
        include_cloud_measure=STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=False,
        include_entry_baseline=STAGE1_INCLUDES_ENTRY_BASELINE,
    ))


# Capture-plan index → phase, the fallback for a session constructed with no
# explicit ``index_phase_map``. APPLYING is a control-page phase with no
# capture, so it has no index. Frozen because a shared module-level default an
# in-place mutation would corrupt for every later session.
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
        3 .. L+2             LATERAL            (L prompted poses)
        L+3 .. L+N+1         CLOUD_MEASURE      (N-1 prompted positions)
        (last)               ENTRY_BASELINE     (#2291's "before", at the mark)

    The lateral walk runs BEFORE any pre-apply cloud because it replays the
    anchor program and is its robustness sample. The entry baseline runs LAST
    because #2291 asks for the summed capture *immediately before apply*; it
    prompts the household back to the mark, so it is one held-still capture.

    There is deliberately no VERIFY entry (#1806): stage 1 applies nothing, so
    nothing post-apply can be measured by it. VERIFY's absence here is what
    ``correction_crossover_v2_status._phase_from_state`` reads to resolve a
    measure-only session to the review interlude.

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

    The prelude announces a SESSION rather than a capture
    (:func:`~.programs.courtesy_prelude_for_phase`), so stage 1 announces its
    first (CHECK) and its last (the entry baseline) and stage 2's walk announces
    its first alone. Derived from the same ``index -> phase`` map the plan's
    entries are built from.
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

    ``plan_shape is None`` is the 1-entry recovery re-verify
    (``{1: PHASE_VERIFY}``). A shape supplies the tier's own post-apply walk:
    express is ``M = 1`` and resolves to the same single-entry map; Full is the
    multi-position walk the after-chart, the post-apply spec verdict and the
    delta probe all read.
    """
    m = 1 if plan_shape is None else plan_shape.verify_capture_target
    mapping = {1: PHASE_VERIFY}
    for offset in range(m - 1):
        mapping[2 + offset] = PHASE_CLOUD_VERIFY
    return mapping


# --------------------------------------------------------------------------- #
# capture plan + session spec
# --------------------------------------------------------------------------- #

# Phone-side recording margin around each program (lead + tail), presentation /
# locator-window data — never a hard deadline (the session runner's timeout_s
# stays the backstop).
CAPTURE_ENTRY_MARGIN_MS = 2000
# The cancelable auto-advance countdown between an accepted CHECK and MEASURE.
AUTO_ADVANCE_COUNTDOWN_S = 5

# Auto-advance policy vocabulary carried in the per-entry ``screen`` field,
# which is opaque to the schema.
AUTO_ADVANCE_TAP = "tap"            # requires the user's tap (first capture)
AUTO_ADVANCE_COUNTDOWN = "countdown"  # auto-begins behind a cancelable countdown
# Armed by the apply-complete host event. Emitted by no plan today — stage 1 has
# no VERIFY entry and stage 2 opens already-applied — and kept only as
# plan-grammar the page and the runner still understand.
AUTO_ADVANCE_ON_APPLY = "on_apply"

# Phone-inactivity budget for the very FIRST begin of a v2 session, before any
# capture: the microphone-check screen's placement instructions take longer to
# read than the general 120 s ``DEFAULT_TIMEOUT_S``. Every later window keeps
# the tight per-phase arm/upload backstop.
V2_FIRST_BEGIN_TIMEOUT_S = 300.0


def v2_first_begin_timeout_s() -> float:
    """The first-begin budget in force — the constant above, env-overridable.

    Out-of-range or unparseable ``JASPER_V2_FIRST_BEGIN_TIMEOUT_S`` values fall
    back to the default. The ceiling is derived from
    ``capture_protocol.MAX_TTL_S``: nothing outliving that sanity ceiling can be
    honoured, whatever this knob says.
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

    ``index`` is the entry's 1-based WIRE index (the relay's own index space),
    not the 0-based ``CapturePlanEntry.index``.
    """
    return f"Measurement {int(index)} of {int(capture_target)}"


def _positioned_prompt(
    prompt: CloudPositionPrompt, shape: V2PlanShape | None,
) -> CloudPositionPrompt:
    """One pose's prompt, in the vocabulary the shape's OPERATOR acts on.

    A tap-paced shape keeps the tape-measure copy verbatim; a gated one restates
    the same pose as its angle. Only the sentence differs — ``offset_cm`` and
    ``role`` are untouched, so a gated session's evidence stays comparable with
    a tape-measured one's.
    """
    if shape is not None and shape.positions_gated:
        return remote_position_prompt(prompt)
    return prompt


def _entry_advance(shape: V2PlanShape | None) -> dict[str, str]:
    """The auto-advance fields one plan entry carries, from its SHAPE.

    Hand-advanced shapes get :data:`AUTO_ADVANCE_TAP` and no countdown key —
    including a hand-RELEASED one, whose begins are gated but whose operator is
    there to tap. Only :attr:`V2PlanShape.externally_positioned` gets the
    countdown, and its begin is then held by the position gate until the driver
    reports the angle reached. ``shape is None`` is the recovery re-verify.

    ``countdown_s`` is a STRING because ``CapturePlanEntry.screen`` is a
    ``str -> str`` map on the wire.
    """
    if shape is not None and shape.externally_positioned:
        return {
            "auto_advance": AUTO_ADVANCE_COUNTDOWN,
            "countdown_s": str(AUTO_ADVANCE_COUNTDOWN_S),
        }
    return {"auto_advance": AUTO_ADVANCE_TAP}


#: The per-entry screen keys that state a GATED entry's TARGET POSITION in
#: machine terms, emitted only by a shape with
#: :attr:`V2PlanShape.positions_gated`. The plan is the source of that angle;
#: the gate and the envelope read it back off the entry.
POSITION_DEG_KEY = "position_deg"
POSITION_ROLE_KEY = "position_role"


def _entry_policy(
    shape: V2PlanShape | None, prompt: CloudPositionPrompt | None = None,
) -> dict[str, str]:
    """One entry's non-copy ``screen`` fields: advance policy + target position.

    ``prompt is None`` means an entry with no prompted pose of its own — CHECK,
    MEASURE, the entry baseline, stage 2's anchor — each a 0° design-axis
    capture, which is what it declares. Those are gated too, so a gated
    session's operator is asked to put the microphone back on the axis rather
    than trusted to have left it there.
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
    """The STAGE-1 (measure) CapturePlan.

    CHECK and MEASURE are required; the pre-apply cloud, the lateral walk and
    the entry baseline are optional. Built from
    :func:`build_v2_cloud_index_phase_map` so prompt and phase cannot disagree.
    When included, the pre-apply cloud ends stage 1 and holds for an explicit
    completion signal; Apply is left to the untimed review interlude, and
    post-apply capture is stage 2's own session.

    Every entry's ``screen`` carries ``progress`` (the server-derived counter),
    ``title`` (one imperative instruction) and ``body`` (at most one supporting
    clause). Entry durations derive from the composed programs plus a lead/tail
    margin — MEASURE is sized from a nominal gain plan, exact because sweep and
    gap lengths are gain-independent.
    """
    from jasper.capture_protocol import CapturePlan, CapturePlanEntry

    roles = tuple(roles_bands)
    # Every program below asks ``courtesy_prelude_for_phase`` for its OWN phase:
    # this is the phone's DURATION BUDGET and must agree with what
    # ``programs``' composers actually play, or the phone stops recording before
    # the prelude-lengthened program ends.
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
    # is the verify program's even though stage 1 runs no VERIFY phase, and it
    # is the announced one because its program object is stage 2's anchor.
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
    # One policy for every entry of this plan. The first entry's value is inert:
    # the page starts round 1 from the spec's own begin button and only reads
    # the policy of the entry AFTER an accepted capture.
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
                # Deliberately NOT "stay quiet": CHECK's ambient window is the
                # session's room-noise measurement and the gain solve reads it,
                # so a pre-hushed room under-drives against the noise the later
                # sweeps actually face. The speaker asks for quiet itself, on
                # the in-sweep windows where it is wanted.
                "body": (
                    "JTS listens to the room exactly as it is first, so carry "
                    "on as you were — it will say when to be quiet."
                ),
                # The phone's own pre-arm floor window is a third measurement:
                # a sub-second reading taken before the speaker plays anything.
                # Absent on every other entry, where the page's quiet default
                # stays right.
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
                # MEASURE is the session's longest capture and can be its
                # loudest: each driver's level is solved to the SNR the fit
                # needs in its own band. It takes a household TAP rather than a
                # countdown (#1823) — that consent ruling binds the hand-walked
                # tiers only, and an externally positioned shape auto-advances
                # behind the position gate. Household language in the copy: say
                # what the level is FOR, never which internal stage asked.
                "body": (
                    "This one is longer, and can be the loudest — it measures "
                    "each driver alone at the level it needs to hear each one "
                    "clearly."
                ),
                **advance,
            },
        ),
    ]
    # ``duration_ms`` is the MEASURE program's because each pose replays it
    # verbatim (``program_for_phase``), not the summed sweep's.
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
    # The "before" measurement, LAST. Its duration is the summed sweep's
    # (``verify_ms``) because it replays the VERIFY program verbatim — the
    # identity ``program_for_phase`` guarantees and the benefit comparison
    # depends on.
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
                    # Household register: no "baseline", no "summed sweep".
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

    ONE resolver, so the plan, the index→phase map and the session's own
    ``_cloud_prompt`` cannot read three different tables. ``None`` is the
    ratified :data:`CLOUD_VERIFY_POSE_PROMPTS`.
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

    ``plan_shape is None`` is the recovery re-verify: one entry,
    ``CAPTURE_PLAN_MAX_ATTEMPTS``, and copy that leads with how cheap it is. A
    ``plan_shape`` builds stage 2 of the two-stage commission flow — VERIFY's
    design-axis anchor at the mark plus ``M − 1`` prompted post-apply positions.
    Express is ``M = 1`` and makes no cross-position claim.

    The anchor entry carries ``confirm_title`` / ``confirm_body``, so the tone
    waits for the household to say they are standing on the mark.

    ``verify_prompts`` is the pose set this walk takes; ``None`` is the ratified
    one (:func:`verify_pose_table`). The shape's ``M`` and the table's length
    must agree — a shape asking for more poses than the table supplies is
    refused here rather than walked short.
    """
    from jasper.capture_protocol import CapturePlan, CapturePlanEntry

    # The anchor is stage 2's OPENING capture, so it is announced; the prompted
    # positions behind it are not. Two nominal programs because the phone
    # budgets each entry from the program that entry will actually record.
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
    # The phone's END screen once every capture completes; the page reads the
    # FINAL wire index's entry, so which entry carries it depends on the tier.
    #
    # Every word here is written BEFORE THE FIRST TONE PLAYS, so nothing on this
    # screen may assert a MEASURED outcome (#1964). It states only what arming
    # establishes: the correction is applied, and reaching this screen means the
    # tracking comparator passed. The post-apply SPEC verdict can fail while
    # tracking passes, and the phone cannot carry that caveat — its component
    # vocabulary has no result-shaped member and the relay's host-event slot is
    # last-write-wins — so the verdict has ONE owner, jts.local's done screen,
    # and this copy points at it rather than guessing it.
    #
    # Express's upgrade-path phrase is COPIED from jts.local
    # (``crossover_envelope_v2``'s express ``done_verdict`` and
    # ``_TIER_CLAIMS[TIER_FULL]``), so a re-wording has one place to start.
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
    # Asked separately: the COPY follows the pose statement, the confirm tap
    # below follows the advance policy.
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
        # The confirm-then-tone tap, on stage 2's own begin. OMITTED for an
        # externally positioned shape: ``entryConfirmsBeforeArming``
        # (capture-page/js/main.js) treats a present ``confirm_title`` as "hold
        # the tone until somebody taps", so an unattended session would park on
        # a confirm screen and burn the runner's ``awaiting_arm`` budget. A
        # hand-RELEASED shape keeps it, which is why this reads the advance
        # policy rather than ``positions_gated``.
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
    # EQUALITY, not "the table is long enough": a longer table is silently
    # walked as a PREFIX, while :func:`build_v2_verify_session_spec` quotes the
    # orientation's reach off the WHOLE table, so the wire would promise a reach
    # the walk never takes. Gated on
    # :attr:`V2PlanShape.has_cloud_verify_group` because express (``M = 1``)
    # emits no cloud-verify entry at all.
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
    """The capture spec for a post-apply session (stage 2, or the recovery).

    The consent surface is chosen by the PLAN's own shape, not by the caller's
    intent. A single-capture plan keeps the stationary consent copy and leads
    with :data:`REVERIFY_NO_REWALK_HEADLINE`; a multi-capture plan takes the
    guided consent surface with its own capture count and tier.
    """
    # Resolved ONCE and handed to both readers below, so the orientation's
    # sentence and the walk's entries come from the same object.
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
            # Quoted off the SAME resolved pose set the entries above were
            # built from, so the sentence cannot describe a reach the walk does
            # not have.
            "walk_shape": cloud_walk_shape(verify_table, post_apply=True),
            # Stage 2 announces its anchor and nothing behind it.
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

    The ceiling grows by :data:`WALL_CLOCK_CEILING_PER_ENTRY_S` for every
    accepted capture beyond the 3-entry baseline and is hard-capped by
    ``session_volume_plan.MAX_WALL_CLOCK_CEILING_S``, which owns that bound. Each
    STAGE arms its own ceiling from its own plan.
    """
    target = int(getattr(capture_plan, "capture_target", CAPTURE_PLAN_TARGET) or 0)
    return wall_clock_ceiling_s(target)


# Per accepted capture beyond the 3-entry baseline: covers a prompt read, a
# deliberate mic move, a tap, the ~16 s sweep entry and the upload. A budget
# allowance, deliberately generous — never a measured position time.
WALL_CLOCK_CEILING_PER_ENTRY_S = 120.0

# A fixed, representative 2-way RoleBand pair for :func:`tier_display_info`
# ONLY — never the household's actual excitation ceilings/topology. The
# tweeter's lower edge is the CONSERVATIVE end of a plausible tweeter
# (~1.5-2 kHz): a too-low f1 biases the estimated minutes SHORT, the wrong
# failure direction for a number the household reads as a promise.
#
# Deliberately NOT derived from the household's declared driver low limit
# (#2603): the only resolution path for it (``resolve_conductor_context``) is
# refuse-if-not-ready and can regenerate the crossover preview file as a SIDE
# EFFECT, which this ~1.5 s-poll display value must not do.
_DISPLAY_ROLES_BANDS = (
    RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
    RoleBand("tweeter", 1, FrequencyBand(1800.0, 20000.0)),
)
_DISPLAY_FC_HZ = 1600.0


def tier_display_info() -> dict[str, dict[str, int]]:
    """Per-tier ``{capture_target, estimated_minutes}`` for the wizard's
    pre-session tier chooser.

    Derived from the same builders a live session uses, but over a fixed
    representative band pair (:data:`_DISPLAY_ROLES_BANDS`), because resolving
    the household's real topology is refuse-if-not-ready and has side effects.
    Realized sweep length does vary with the band; what makes the fixed pair
    honest is that :meth:`CapturePlan.estimated_minutes`' ceil-to-whole-minutes
    quantum absorbs that variance across the plausible 2-way band space, which
    ``test_tier_display_info_minutes_hold_across_plausible_topologies`` sweeps.
    That invariant is empirical, not structural, and would need re-deriving if
    the plausible band space widened. The figure itself is this function's
    output; do not write it down elsewhere.

    Memoized because the inputs are module constants.
    :func:`functools.lru_cache` does not cache an exception, so the try/except
    below is a one-time fallback for that residual path, not a per-poll retry.
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
                # Present even here: the chooser's copy reads both keys.
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
        # BOTH stages: ``capture_target`` is the whole journey's count, so the
        # duration must be too. Two ceils rather than one is deliberately
        # conservative — the household really does pay two per-session set-ups.
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
            # Off the SHAPE's own two targets — the same properties the plan
            # builders size themselves from — so their sum is
            # ``capture_target`` by construction and the fallback path above can
            # answer with the same numbers.
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
    """One stage-1 capture spec, optionally including the pre-apply cloud.

    Rides :func:`~.sweep_spec.build_crossover_sweep_spec` with its stage-1 plan
    attached, and selects guided consent only for a plan that prompts a move —
    that builder's default wording promises a stationary mic for the whole
    session. The spec-level stimulus duration is the longest entry, so the
    per-capture deadline covers every phase.
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
    # EITHER group makes this a walk. The entry baseline is deliberately NOT a
    # third term: it is one capture at the mark the household is already
    # standing at, and ``walk_shape_for`` computes a 0 cm reach for it, so
    # claiming ``walked`` would emit guided consent with no shape line under it.
    walked = include_cloud_measure or include_lateral
    return build_crossover_sweep_spec(
        driver_label="crossover",
        driver_role="summed",
        acknowledgement_binding=acknowledgement_binding,
        stimulus_duration_ms=longest_ms,
        capture_plan=plan,
        # Every capture the household is prompted through.
        guided_captures=plan.capture_target if walked else 0,
        # …and WHICH of those announce themselves.
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
        # …and which INSTRUMENT that walk is, so the spec builder need not
        # re-derive a shape it does not own.
        guided_tier=shape.tier if walked else "",
        # …and how far the walk reaches: the FURTHEST of whichever groups run,
        # from the same table the per-entry screens above are built from.
        walk_shape=walk_shape_for(
            cloud_positions=(
                shape.cloud_measure_positions if include_cloud_measure else 0
            ),
            lateral=include_lateral,
            lateral_prompts=lateral_prompts,
        ),
        **spec_kwargs,
    )
