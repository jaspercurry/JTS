# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What one ``measure`` asks for, and what the engine admits it cannot do yet.

The mic-only parameter surface ships COMPLETE, with the unbuilt regimes as loud
stubs — a value returned to the caller, never a log line and never a raise
(ruling S12 -- see ADR-0228). A preset is a saved :class:`MeasureSpec` and
nothing more. The vocabulary is copied from
:mod:`.contracts` rather than imported from its owners, which cost ~1,100
modules including ``numpy`` on a 1 GB Pi.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from typing import Any, Mapping

from jasper.audio_measurement.null_walk import MAX_DSP_DELAY_US
from jasper.json_fields import finite_float

from .contracts import (
    DRIVER_ROLES,
    MEASURE_KINDS,
    MEASURE_REGIMES,
    POLARITIES,
    POLARITY_INVERTED,
    POLARITY_NORMAL,
    POSITION_AXIS_HORIZONTAL,
    POSITION_AXIS_VERTICAL,
    REGIME_NEAR_FIELD,
    REGIME_REFERENCE_AXIS,
)
from .journey import CAPTURE_PHASES

__all__ = [
    "DISTORTION_VS_LEVEL_NOT_IMPLEMENTED",
    "NEAR_FIELD_SPLICE_NOT_IMPLEMENTED",
    "STUB_CODES",
    "VERTICAL_AXIS_NOT_IMPLEMENTED",
    "CapabilityStub",
    "MeasureSpec",
    "CANDIDATE_SCOPES",
    "GRAPH_SCOPES",
    "GRAPH_SCOPE_DRIVERS",
    "inverted_roles_for",
    "level_trims_for",
    "measurement_delays_for",
    "stubbed_capabilities",
]

#: R-3. The near-field capture ships; the splice onto the far-field trace is
#: the analysis that does not exist.
NEAR_FIELD_SPLICE_NOT_IMPLEMENTED = "near_field_splice_not_implemented"
#: R-4. Every rung of the ladder plays and banks its own record; what does not
#: exist is the consumer that turns the set into a measured floor.
DISTORTION_VS_LEVEL_NOT_IMPLEMENTED = "distortion_vs_level_not_implemented"
#: R-5a. A vertical pose plays and banks, labelled with the elevation the
#: operator was asked for (:attr:`~.spatial.PositionGeometry.vertical_deg`).
#: What does not exist is the consumer that reads lobing out of an elevation set.
VERTICAL_AXIS_NOT_IMPLEMENTED = "vertical_axis_not_implemented"


@dataclass(frozen=True)
class CapabilityStub:
    """One named hole in the engine's own capability, said out loud.

    ``captured`` is the operationally load-bearing field: a stub whose capture
    still happened has evidence waiting for the analysis that will read it, and
    one whose capture did not happen has nothing banked. ``instrument`` names
    the §5 roster row that closes the hole.
    """

    code: str
    instrument: str
    captured: bool
    message: str


@dataclass(frozen=True)
class _StubRow:
    """The four facts one hole is rendered from."""

    capability: str
    owed: str
    instrument: str
    captured: bool


def _stub(code: str, row: _StubRow) -> CapabilityStub:
    """One stub in the wording shape ruling S12 fixes."""
    banked = "capture banked" if row.captured else "nothing captured"
    return CapabilityStub(
        code=code,
        instrument=row.instrument,
        captured=row.captured,
        message=(
            f"{row.capability} not implemented; {banked}, "
            f"{row.owed} pending {row.instrument}"
        ),
    )


#: One row per named hole, kept as data so a fifth stub joins the engine's
#: vocabulary by adding a row here and nowhere else.
_ROWS: dict[str, _StubRow] = {
    NEAR_FIELD_SPLICE_NOT_IMPLEMENTED: _StubRow(
        "near-field splice", "splice", "R-3", captured=True,
    ),
    DISTORTION_VS_LEVEL_NOT_IMPLEMENTED: _StubRow(
        "distortion-vs-level sweep", "level ladder", "R-4", captured=True,
    ),
    VERTICAL_AXIS_NOT_IMPLEMENTED: _StubRow(
        "vertical-axis analysis", "elevation read", "R-5a", captured=True,
    ),
}

_STUBS = {code: _stub(code, row) for code, row in _ROWS.items()}

#: Every code :func:`stubbed_capabilities` can return, so a caller can CHECK a
#: code rather than trust it. Derived from the table above, never re-listed.
STUB_CODES = frozenset(_STUBS)
GRAPH_SCOPE_DRIVERS = "drivers"
CANDIDATE_SCOPES = frozenset({"candidate", "candidate_branches"})
GRAPH_SCOPES = (GRAPH_SCOPE_DRIVERS, *sorted(CANDIDATE_SCOPES))


@dataclass(frozen=True)
class MeasureSpec:
    """The parameter bundle one ``measure`` runs, and one preset saves.

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
    single stimulus the program declares.

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
    regime: str = REGIME_REFERENCE_AXIS
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
    program_phase: str = ""

    def __post_init__(self) -> None:
        if self.graph_scope not in GRAPH_SCOPES:
            raise ValueError(f"graph_scope must be one of {GRAPH_SCOPES}")
        if self.graph_scope in CANDIDATE_SCOPES and not self.candidate_id.strip():
            raise ValueError(f"{self.graph_scope} graph_scope requires candidate_id")
        if self.sweep_band_hz:
            if self.graph_scope == GRAPH_SCOPE_DRIVERS:
                raise ValueError("sweep_band_hz requires a summed graph_scope")
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
        if self.regime not in MEASURE_REGIMES:
            raise ValueError(
                f"a capture regime must be one of {MEASURE_REGIMES}, "
                f"got {self.regime!r}"
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
        """Axis, bearing and elevation, checked by the module that owns the frame.

        The import is deferred because :mod:`.spatial` costs ~1,100 modules
        including ``numpy``; only the paths that state a pose pay for it.
        """
        from .spatial import MARK_DISTANCE_M, PositionGeometry

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
    "kind", "position_axis", "regime", "polarity", "inverted_role",
    "candidate_id", "delayed_role", "graph_scope", "program_phase",
})
_ARRAYS = frozenset({"positions", "pose_prompts", "level_ladder_dbfs", "sweep_band_hz"})
#: ``sweep_s`` read ``None`` back as the statement it is.
_NUMBERS = frozenset({"delay_us", "sweep_s"})
#: Read back as banked; the dataclass judges them.
_PASSTHROUGH = _FIELD_NAMES - _TRIMMED_STRINGS - _ARRAYS - _NUMBERS


def _finite(name: str, value: Any) -> float:
    number = finite_float(value)
    if number is None:
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return number


def _from_json(name: str, value: Any) -> Any:
    """One banked value, typed as the flag door would have typed it.

    What is NOT typed here carries no shape JSON can get wrong on its own:
    ``positions`` entries are whole degrees, which ``__post_init__`` judges.
    """
    if name in _TRIMMED_STRINGS:
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string, got {value!r}")
        return value.strip()
    if name == "level_matched":
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
        if name == "pose_prompts" and not all(
            isinstance(entry, str) for entry in value
        ):
            raise ValueError(f"{name} entries must be strings, got {value!r}")
        if name in ("level_ladder_dbfs", "sweep_band_hz"):
            return tuple(_finite(name, entry) for entry in value)
        return tuple(value)
    if name in _NUMBERS:
        return None if value is None else _finite(name, value)
    return value


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


def stubbed_capabilities(spec: MeasureSpec) -> tuple[CapabilityStub, ...]:
    """Every capability this spec asks for that the engine has not built.

    Total and side-effect-free. :meth:`~.session.TuningSession.measure` reads
    ``captured=False`` as "there is nothing to play" and ``captured=True`` as
    "play, bank, and say what the banked evidence is still owed".
    """
    codes: list[str] = []
    if spec.regime == REGIME_NEAR_FIELD:
        codes.append(NEAR_FIELD_SPLICE_NOT_IMPLEMENTED)
    if spec.level_ladder_dbfs:
        codes.append(DISTORTION_VS_LEVEL_NOT_IMPLEMENTED)
    if spec.position_axis == POSITION_AXIS_VERTICAL or spec.vertical_deg:
        # Keyed on the ELEVATION, not on the axis word: the two are orthogonal,
        # so a horizontal walk raised off mark height banks the same unanalysed
        # evidence a vertical walk does.
        codes.append(VERTICAL_AXIS_NOT_IMPLEMENTED)
    return tuple(_STUBS[code] for code in codes)
