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

from jasper.audio_measurement.null_walk import MAX_DSP_DELAY_US
from jasper.platform.json_fields import require_finite
from jasper.platform.speaker_layout import measurement_target_id

from ..measurement_programs import BRANCH_PAIR_FRONT_REAR, CANDIDATE_LAYERS, validated_stimulus
from ..test_signal_plan import MAX_DRIVER_TEST_FREQUENCY_HZ, MIN_DRIVER_TEST_FREQUENCY_HZ
from .contracts import (
    DRIVER_ROLES,
    DRIVER_ROLE_WOOFER,
    MEASURE_KIND_CANDIDATE,
    MEASURE_KINDS,
    POLARITIES,
    POLARITY_INVERTED,
    POLARITY_NORMAL,
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
    "inverted_roles_for",
    "level_trims_for",
    "measurement_delays_for",
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

    The polarity flip is RELATIVE to the design polarity the graph would
    otherwise carry, so a ``polarity=inverted`` record can name a graph whose
    source reads ``inverted: false`` by double negation. The emitted
    ``# inverted_roles=[…]`` metadata comment is what disambiguates the pair;
    never the flag.
    """

    kind: str
    positions: tuple[int, ...] = ()
    pose_prompts: tuple[str, ...] = ()
    position_axis: str = POSITION_AXIS_HORIZONTAL
    vertical_deg: int = 0
    polarity: str = POLARITY_NORMAL
    inverted_role: str = ""
    level_ladder_dbfs: tuple[float, ...] = ()
    sweep_band_hz: tuple[float, float] | tuple[()] = ()
    sweep_s: float | None = None
    candidate_id: str = ""
    #: R-1's delay coordinate: which branch carries it, and how much. The pair
    #: behaves like ``polarity``/``inverted_role`` — stating one without the
    #: other is a spec that means two things. Zero on every other capture, which
    #: is what keeps their graphs byte-identical.
    delayed_role: str = ""
    delay_us: float = 0.0
    #: Whether this capture's graph carries the box's own per-driver level-match
    #: trims. A BOOLEAN and never the numbers: the trims are resolved on-box from
    #: banked evidence at the one precedence owner, so hand-carried values would
    #: measure through some other box's level match. False on every other
    #: capture, which is what keeps their graphs byte-identical.
    level_matched: bool = False
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
    #: is asked: a driver's take, or the first take of a driverless summed set
    #: closer than the mark. The first take of a branch set plays its branches'
    #: probes instead (:func:`branch_probes`). Only
    #: ``capture_schedule.prepare_plan_captures`` sets it (ADR-0365, ADR-0403).
    level_probe: bool = False
    #: The level each branch of a ``candidate_branches`` take plays alone at, in
    #: ``branch_target_ids`` order, in dBFS; ``level_ladder_dbfs`` plays their sum.
    #: Only the executor sets it, from the branches' probes (ADR-0407).
    branch_levels_dbfs: tuple[float, ...] = ()
    #: What this take's graph keeps on each output for its dynamic bass boost, dB, by
    #: measurement target: a branch alone plays under its own cap less it (ADR-0359,
    #: ADR-0407). Only the composition seam sets it, from the take's own graph.
    bass_reserve_db: Mapping[str, float] | None = None

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
        if self.graph_scope != GRAPH_SCOPE_DRIVERS and (
            self.polarity != POLARITY_NORMAL or self.inverted_role
            or self.delayed_role or self.delay_us or self.level_matched
        ):
            raise ValueError("graph overlays require drivers graph_scope")
        if self.program_phase and self.program_phase not in CAPTURE_PHASES:
            raise ValueError(f"program_phase must be one of {CAPTURE_PHASES}")
        if self.kind not in MEASURE_KINDS:
            raise ValueError(
                f"a measure kind must be one of {MEASURE_KINDS}, got {self.kind!r}"
            )
        if self.polarity not in POLARITIES:
            raise ValueError(
                f"a capture polarity must be one of {POLARITIES}, "
                f"got {self.polarity!r}"
            )
        if self.polarity == POLARITY_INVERTED:
            if self.inverted_role not in DRIVER_ROLES:
                raise ValueError(
                    "an inverted-polarity capture must name the driver branch "
                    f"it flips, one of {DRIVER_ROLES}, got "
                    f"{self.inverted_role!r}"
                )
        elif self.inverted_role:
            raise ValueError(
                f"inverted_role={self.inverted_role!r} needs "
                f"polarity={POLARITY_INVERTED!r}; a {self.polarity!r} capture "
                "flips no branch"
            )
        if bool(self.delayed_role) != bool(self.delay_us):
            raise ValueError(
                "delayed_role and delay_us are one decision with two halves: "
                f"got delayed_role={self.delayed_role!r} with "
                f"delay_us={self.delay_us!r}"
            )
        if self.delayed_role and self.delayed_role not in DRIVER_ROLES:
            # An unknown role emits a Delay filter the pipeline never
            # references, so the capture plays with NO delay and banks as a
            # delayed take.
            raise ValueError(
                "a delayed capture must name a real driver branch, one of "
                f"{DRIVER_ROLES}, got {self.delayed_role!r}"
            )
        if not math.isfinite(self.delay_us):
            raise ValueError(f"delay_us must be finite, got {self.delay_us!r}")
        if self.delay_us < 0.0 or self.delay_us > MAX_DSP_DELAY_US:
            # The sign frame lives in the walk coordinate, which names the
            # branch; what reaches a Delay filter is always non-negative.
            raise ValueError(
                f"delay_us is a non-negative microsecond value at or below "
                f"{MAX_DSP_DELAY_US:g}, got {self.delay_us!r}"
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
        """This spec as the JSON object :meth:`from_mapping` reads back.

        Every field is written, tuples as arrays: a reader never has to guess
        which default a writer was holding.
        """
        return {
            spec_field.name: (
                list(value) if isinstance(value, tuple) else value
            )
            for spec_field in fields(self)
            for value in (getattr(self, spec_field.name),)
        }

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> MeasureSpec:
        """One spec from a JSON object, with the typing JSON does not carry.

        The field set is CLOSED: an unknown key is a misspelling rather than an
        extension. Arrays become tuples, strings are trimmed, and a number
        spelled as a word is refused -- a spec read from a document is judged
        exactly as one built from flags, and every refusal is this class's own
        ``ValueError`` rather than a ``TypeError``/``KeyError`` no door catches.
        """
        unknown = sorted(set(mapping) - _FIELD_NAMES)
        if unknown:
            raise ValueError(f"not MeasureSpec fields: {', '.join(unknown)}")
        missing = sorted(_REQUIRED_FIELDS - set(mapping))
        if missing:
            raise ValueError(f"a spec must state {', '.join(missing)}")
        return cls(**{
            name: _from_json(name, value) for name, value in mapping.items()
        })

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


_FIELD_NAMES = frozenset(spec_field.name for spec_field in fields(MeasureSpec))

#: The fields with no default: a mapping omitting one states no spec at all.
_REQUIRED_FIELDS = frozenset({"kind"})

_TRIMMED_STRINGS = frozenset({
    "kind", "position_axis", "polarity", "inverted_role",
    "candidate_id", "delayed_role", "graph_scope", "program_phase",
})
_ARRAYS = frozenset({"positions", "pose_prompts", "level_ladder_dbfs", "sweep_band_hz",
                     "branch_target_ids", "cleared_layers", "branch_levels_dbfs"})
#: ``sweep_s`` read ``None`` back as the statement it is.
_NUMBERS = frozenset({"delay_us", "sweep_s"})
#: Read back as banked; the dataclass judges them.
_PASSTHROUGH = _FIELD_NAMES - _TRIMMED_STRINGS - _ARRAYS - _NUMBERS


def _from_json(name: str, value: Any) -> Any:
    """One banked value, typed as the flag door would have typed it.

    What is NOT typed here carries no shape JSON can get wrong on its own:
    ``positions`` entries are whole degrees, which ``__post_init__`` judges.
    """
    if name in _TRIMMED_STRINGS:
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string, got {value!r}")
        return value.strip()
    if name in ("level_matched", "level_probe"):
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be true or false, got {value!r}")
        return value
    if name == "vertical_deg":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be a whole number, got {value!r}")
        return value
    if name in _ARRAYS:
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{name} must be a JSON array, got {value!r}")
        if name in ("pose_prompts", "branch_target_ids", "cleared_layers") and not all(
            isinstance(entry, str) for entry in value
        ):
            raise ValueError(f"{name} entries must be strings, got {value!r}")
        if name in ("level_ladder_dbfs", "sweep_band_hz", "branch_levels_dbfs"):
            return tuple(require_finite(entry, field=name) for entry in value)
        return tuple(value)
    if name in _NUMBERS:
        return None if value is None else require_finite(value, field=name)
    return value


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


def measurement_delays_for(spec: MeasureSpec) -> dict[str, float]:
    """The per-role delay map this spec's graph must carry.

    Empty for every spec that names no delay, which is what keeps an ordinary
    program's graph byte-identical.
    """
    if not spec.delayed_role:
        return {}
    return {spec.delayed_role: spec.delay_us}


def level_trims_for(
    spec: MeasureSpec, resolved_db: Mapping[str, float] | None,
) -> dict[str, float]:
    """The per-role attenuation this spec's graph must carry.

    ``resolved_db`` is what the session was opened with — resolved once, on-box,
    from the banked evidence the box owns — so this chooses between applying it
    and applying nothing, and never derives a value. A spec asking for a level
    match when the session holds no trims answers empty rather than raising:
    that refusal belongs at session open, where an operator can still act on it.
    """
    if not spec.level_matched:
        return {}
    return {str(role): float(db) for role, db in (resolved_db or {}).items()}


def inverted_roles_for(spec: MeasureSpec) -> tuple[str, ...]:
    """The driver branches this spec's graph must carry sign-flipped.

    Empty for every normal-polarity spec, which is what keeps a non-inverted
    install byte-identical to what it always emitted.
    """
    if spec.polarity != POLARITY_INVERTED:
        return ()
    return (spec.inverted_role,)
