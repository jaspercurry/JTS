# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What one ``measure`` asks for (ruling S12 -- see ADR-0228).

The vocabulary is copied from :mod:`.contracts` rather than imported from its
owners, which cost ~1,100 modules including ``numpy`` on a 1 GB Pi.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields, replace
from typing import Any, Mapping, Sequence

from jasper.platform.json_fields import finite_float
from jasper.platform.speaker_layout import measurement_target_id

from ..measurement_programs import BRANCH_PAIR_FRONT_REAR, CANDIDATE_LAYERS, validated_stimulus
from ..test_signal_plan import MAX_DRIVER_TEST_FREQUENCY_HZ, MIN_DRIVER_TEST_FREQUENCY_HZ
from .contracts import (
    DRIVER_ROLE_WOOFER,
    MEASURE_KIND_CANDIDATE,
    MEASURE_KINDS,
    POSITION_AXIS_HORIZONTAL,
    POSITION_AXIS_VERTICAL,
)
from .journey import CAPTURE_PHASES

__all__ = [
    "MeasureSpec",
    "CANDIDATE_SCOPES",
    "GRAPH_SCOPES",
    "GRAPH_SCOPE_DRIVERS",
    "branch_channels_for",
    "branch_probes",
    "branch_target_ids_for",
]

GRAPH_SCOPE_DRIVERS = "drivers"
CANDIDATE_SCOPES = frozenset({"candidate", "candidate_branches", "timing"})
GRAPH_SCOPES = (GRAPH_SCOPE_DRIVERS, *sorted(CANDIDATE_SCOPES))


@dataclass(frozen=True)
class MeasureSpec:
    """The parameter bundle one ``measure`` runs.

    ``positions`` are signed whole-degree bearings on ``position_axis``, in the
    frame :class:`~.spatial.PositionGeometry` declares and owns: negative is
    LEFT of the design axis as seen from the microphone looking at the speaker.
    ``()`` and ``(0,)`` both name the design axis alone
    (:data:`~.contracts.DESIGN_AXIS_DEG`) and produce one record shape, never
    two spellings of the same place. ``pose_prompts`` is what the mover was
    TOLD, one per position, and it is the ``place`` block's ``prompt`` field;
    nothing here names the mover (ADR-0228 §8).

    ``vertical_deg`` is one signed whole-degree elevation above mark height for
    the whole spec, in the same frame. Nothing on this rig swings in elevation,
    so a vertical walk states no ``positions``.

    ``level_ladder_dbfs`` rungs are stimulus levels in dBFS: the ladder moves
    the STIMULUS and never the claim, which is what ruling S8's "same drive
    voltage, nothing touched between measurements" rests on. Empty means the
    single stimulus the program declares, or with ``level_probe`` its level
    probe (ADR-0365, ADR-0403).
    """

    kind: str
    positions: tuple[int, ...] = ()
    pose_prompts: tuple[str, ...] = ()
    position_axis: str = POSITION_AXIS_HORIZONTAL
    vertical_deg: int = 0
    level_ladder_dbfs: tuple[float, ...] = ()
    sweep_band_hz: tuple[float, float] | tuple[()] = ()
    sweep_s: float | None = None
    candidate_id: str = ""
    graph_scope: str = GRAPH_SCOPE_DRIVERS
    #: Per-target level offsets of this take's graph against the level anchor's
    #: graph. None: no reference is known, so blind pilots keep their fixed cut.
    scope_gains_db: Mapping[str, float] | None = None
    program_phase: str = ""
    stimulus: Mapping[str, Any] | None = None
    #: The measurement target ids this take excites, in program-channel order
    #: (:func:`branch_channels_for`): two on a ``candidate_branches`` take — the
    #: declared driver pair, or a cabinet's front and rear woofer — or ONE on a
    #: drivers take that plays that target alone, every other declared target
    #: parked. The take's own choice, so it travels on the spec rather than
    #: being re-derived from the box's acoustic roles. Empty otherwise.
    branch_target_ids: tuple[str, ...] = ()
    #: The applied candidate layers this take's purpose clears, emptied by the
    #: door when it compiles the take's graph (ADR-0370). Candidate graphs only.
    cleared_layers: tuple[str, ...] = ()
    #: Whether this take finds its level, playing its level probe when no level
    #: is asked: a driver's take, the first take of a driverless summed set closer
    #: than the mark, or a candidate graph's first summed take at any spot (ADR-0423).
    #: The first take of a branch set plays its branches'
    #: probes instead (:func:`branch_probes`). Only
    #: ``capture_schedule.prepare_plan_captures`` sets it (ADR-0365, ADR-0403).
    level_probe: bool = False
    #: The level each branch of a ``candidate_branches`` take plays alone at, in
    #: ``branch_target_ids`` order, in dBFS; ``level_ladder_dbfs`` plays their sum.
    #: Only the executor sets it, from the branches' probes (ADR-0407).
    branch_levels_dbfs: tuple[float, ...] = ()
    #: What this take's graph keeps on each output for its dynamic bass boost, dB, by
    #: measurement target: each branch alone plays under the tightest of the take's
    #: caps less it (ADR-0359, ADR-0407). Only the composition seam sets it, from the
    #: take's own graph.
    bass_reserve_db: Mapping[str, float] | None = None
    #: Whether this take plays the courtesy prelude: its run's first take
    #: (:func:`~.capture_plan.announce_run`). A level probe never plays it (ADR-0417).
    courtesy_prelude: bool = False

    def __post_init__(self) -> None:
        if self.stimulus is not None:
            solo = bool(solo_target(self))
            validated_stimulus(self.stimulus, one_driver=solo)
            if not solo and self.graph_scope != "candidate":
                raise ValueError("a planned stimulus plays on the candidate graph or on one driver alone")
        if self.graph_scope not in GRAPH_SCOPES:
            raise ValueError(f"graph_scope must be one of {GRAPH_SCOPES}")
        if self.graph_scope in CANDIDATE_SCOPES and not self.candidate_id.strip():
            raise ValueError(f"{self.graph_scope} graph_scope requires candidate_id")
        ids = self.branch_target_ids
        if self.graph_scope == "candidate_branches":
            if len(ids) != 2 or len(set(ids)) != 2 or not all(ids):
                raise ValueError(
                    "a candidate_branches capture names two distinct measurement "
                    f"target ids, got {ids!r}"
                )
        elif self.graph_scope == GRAPH_SCOPE_DRIVERS:
            if len(ids) > 1 or not all(ids):
                raise ValueError(f"a drivers capture excites at most one named target, got {ids!r}")
        elif ids:
            raise ValueError(
                f"branch_target_ids requires the candidate_branches or drivers graph_scope, got {self.graph_scope!r}"
            )
        if self.branch_levels_dbfs and (self.graph_scope != "candidate_branches"
                                        or len(self.branch_levels_dbfs) != len(ids)):
            raise ValueError(f"branch_levels_dbfs names one level per branch of a candidate_branches take, "
                             f"got {self.branch_levels_dbfs!r} on {self.graph_scope!r}")
        if self.bass_reserve_db is not None and not (isinstance(self.bass_reserve_db, Mapping) and all(
                isinstance(target, str) and finite_float(reserve) is not None and reserve >= 0.0
                for target, reserve in self.bass_reserve_db.items())):
            raise ValueError(f"bass_reserve_db names a finite reserve of 0 dB or more per target, "
                             f"got {self.bass_reserve_db!r}")
        if self.cleared_layers and (self.graph_scope not in CANDIDATE_SCOPES
                                    or not set(self.cleared_layers) <= set(CANDIDATE_LAYERS)):
            raise ValueError(f"cleared_layers names layers of a candidate graph, each one of {CANDIDATE_LAYERS}, "
                             f"got {self.cleared_layers!r} on {self.graph_scope!r}")
        if self.sweep_band_hz:
            if self.graph_scope == GRAPH_SCOPE_DRIVERS and not solo_target(self):
                raise ValueError("sweep_band_hz requires a summed graph_scope or one driver alone")
            if len(self.sweep_band_hz) != 2 or not (
                0 < self.sweep_band_hz[0] < self.sweep_band_hz[1] < 24_000
            ):
                raise ValueError("sweep_band_hz must be two ascending values below Nyquist")
        if self.sweep_s is not None:
            if self.graph_scope == GRAPH_SCOPE_DRIVERS:
                raise ValueError("sweep_s requires a summed graph_scope")
            if isinstance(self.sweep_s, bool) or not math.isfinite(self.sweep_s) or self.sweep_s <= 0:
                raise ValueError("sweep_s must be finite and positive")
        if self.program_phase and self.program_phase not in CAPTURE_PHASES:
            raise ValueError(f"program_phase must be one of {CAPTURE_PHASES}")
        if self.kind not in MEASURE_KINDS:
            raise ValueError(
                f"a measure kind must be one of {MEASURE_KINDS}, got {self.kind!r}"
            )
        for bearing in self.positions:
            # Whole degrees, for the reason `PositionGeometry` gives: the poses
            # come from tape-measure offsets to a mark placed "about" 1 m out.
            # `bool` is an `int` and is never a bearing.
            if isinstance(bearing, bool) or not isinstance(bearing, int):
                raise ValueError(
                    "a pose bearing is a whole number of degrees, got "
                    f"{bearing!r}"
                )
        if self.positions and self.position_axis == POSITION_AXIS_VERTICAL:
            # ``positions`` are HORIZONTAL bearings and nothing on this rig
            # commands one on a vertical walk. The invariant downstream readers
            # rely on: a vertical walk's takes carry no bearing, which is what
            # keeps them out of every pooled bearing set.
            raise ValueError(
                f"a {POSITION_AXIS_VERTICAL!r} walk commands no horizontal "
                f"bearing, so it states no positions; got {self.positions!r}. "
                "State where the microphone was raised to with vertical_deg"
            )
        if self.pose_prompts and len(self.pose_prompts) != len(self.positions or (0,)):
            raise ValueError(
                "pose_prompts must name every position or none: "
                f"{len(self.pose_prompts)} prompts for "
                f"{len(self.positions or (0,))} positions"
            )
        self._check_pose_axis()

    def to_dict(self) -> dict[str, Any]:
        """This spec as a JSON object: every field, tuples as arrays."""
        return {
            spec_field.name: (
                list(value) if isinstance(value, tuple) else value
            )
            for spec_field in fields(self)
            for value in (getattr(self, spec_field.name),)
        }

    def _check_pose_axis(self) -> None:
        """Axis, bearing and elevation, checked by the module that owns the frame."""
        from .spatial import MARK_DISTANCE_M, PositionGeometry  # lazy: spatial imports NumPy; only pose checks pay for it

        bearings: tuple[int | None, ...] = self.positions or (None,)
        for bearing in bearings:
            PositionGeometry(
                axis=self.position_axis,
                degrees=bearing,
                mark_distance_m=MARK_DISTANCE_M,
                vertical_deg=self.vertical_deg,
            )


def branch_target_ids_for(branch_pair: str, roles_bands: Sequence[Any]) -> tuple[str, ...]:
    """The two measurement targets one branch pair excites, in channel order.

    ``front_rear`` is spelled rather than resolved from the box: ``rear`` is a
    woofer-only output variant by schema (ADR-0316), so there is exactly one id
    it can name. Every other pair is the box's declared driver roles, lowest
    first, which is what the crossover branch take has always played.
    """
    if branch_pair == BRANCH_PAIR_FRONT_REAR:
        return (DRIVER_ROLE_WOOFER, measurement_target_id(DRIVER_ROLE_WOOFER, "rear"))
    return tuple(band.role for band in roles_bands)


def branch_probes(spec: MeasureSpec) -> tuple[MeasureSpec, ...]:
    """What a branch take that finds its level plays before it: each branch
    alone on the drivers graph, the probe a driver's pose plays, over the take's
    own band, as a near-field probe sweeps its row's (ADR-0360 §4, ADR-0365,
    ADR-0403 §3). A take that states no band sweeps every role's whole band, so
    each probe then sweeps its own driver's. Empty for any other take."""
    if not (spec.level_probe and spec.graph_scope == "candidate_branches"):
        return ()
    band_hz = spec.sweep_band_hz or (MIN_DRIVER_TEST_FREQUENCY_HZ, MAX_DRIVER_TEST_FREQUENCY_HZ)
    return tuple(replace(spec, kind=MEASURE_KIND_CANDIDATE, graph_scope=GRAPH_SCOPE_DRIVERS, candidate_id="",
                         branch_target_ids=(target,), sweep_band_hz=band_hz, sweep_s=None, cleared_layers=(),
                         scope_gains_db=None, level_ladder_dbfs=())
                 for target in spec.branch_target_ids)


def solo_target(spec: MeasureSpec) -> str:
    """The one target a drivers take plays alone, or ``""`` when it plays the
    session's own driver roles."""
    return spec.branch_target_ids[0] if spec.graph_scope == GRAPH_SCOPE_DRIVERS and spec.branch_target_ids else ""


def branch_channels_for(spec: MeasureSpec) -> dict[str, int]:
    """Which program channel carries each target this take names.

    THE single owner: the composers, the capture-window sizer and the graph
    emitter all read this, so no two of them can disagree about what a take
    excites. Empty when the take names no target: one mono program then
    reaches every driver, or a drivers take plays the session's own roles.
    """
    return {target_id: channel for channel, target_id in enumerate(spec.branch_target_ids)}
