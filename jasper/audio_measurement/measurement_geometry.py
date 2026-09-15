# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator-declared measurement rig geometry.

On JTS's rig class the first bounce arrives only ~2.5-3 ms after the direct
sound, so :mod:`.gating`'s reflection finder never fires and the entanglement
floor is DERIVED from declared geometry instead (#3502).
``jasper-declare-geometry`` is the single writer of :data:`DEFAULT_PATH`;
consumers read it at the point of use and never cache it.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Iterable, Mapping

from .gating import ENTANGLEMENT_SOURCE_DECLARED, f_entanglement_floor_hz
from .null_walk import DEFAULT_SOUND_SPEED_M_S
from ..atomic_io import atomic_write_json

DEFAULT_PATH = "/var/lib/jasper/measurement_geometry.json"

#: 1 international inch, exactly.
METERS_PER_INCH = 0.0254

MIN_HEIGHT_M = 0.1
MAX_HEIGHT_M = 3.0
MIN_DISTANCE_M = 0.15
MAX_DISTANCE_M = 3.0
MAX_CEILING_M = 6.0
MIN_WALL_M = 0.05
MAX_WALL_M = 10.0

_LEGACY_WALL_FIELDS = {"front": "front_wall_m", "side": "side_wall_m"}

#: How far below the direct sound one wall's quarter-wave null may be reported.
#: A rigid wall's null is unbounded; a real wall absorbs and diffuses, so the
#: measured dip has a floor.
BOUNDARY_PRIOR_NULL_FLOOR_DB = -20.0

_NULL_FLOOR_MAGNITUDE = 10.0 ** (BOUNDARY_PRIOR_NULL_FLOOR_DB / 20.0)


class GeometryFieldError(ValueError):
    """A refusal that names the offending field as data, not only as prose."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def _number(name: str, value: object) -> float:
    """JSON booleans are not geometry measurements."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GeometryFieldError(
            name, f"{name} must be a number (got {value!r})"
        )
    return float(value)


def _require_range(name: str, value: object, lo: float, hi: float, unit: str = "m") -> None:
    number = _number(name, value)
    if not math.isfinite(number) or not (lo <= number <= hi):
        raise GeometryFieldError(
            name, f"{name} must be within [{lo:g}, {hi:g}] {unit} (got {number:g})"
        )


@dataclass(frozen=True)
class DeclaredGeometry:
    """Operator-declared rig geometry: two heights, a distance, the room around it.

    Every optional field is ``None`` when nothing was declared, never ``0``: the
    ceiling-bounce family is then absent from :meth:`first_bounce_s`'s minimum,
    and an undeclared wall is absent from :func:`boundary_prior`.
    ``cabinet_back_wall_m`` is perpendicular from rear-panel centre to the wall
    behind it. Depth joins rear/front panel centres; toe-in is relative to the
    wall normal (zero faces straight away). Legacy ``front_wall_m`` retains its
    baffle-to-wall meaning; ``side_wall_m`` retains speaker-to-nearest-side-wall.
    See ADR-0317.
    """

    speaker_height_m: float
    mic_height_m: float
    distance_m: float
    ceiling_height_m: float | None = None
    front_wall_m: float | None = None
    side_wall_m: float | None = None
    cabinet_back_wall_m: float | None = None
    cabinet_depth_m: float | None = None
    toe_in_degrees: float | None = None

    def __post_init__(self) -> None:
        _require_range("speaker_height_m", self.speaker_height_m, MIN_HEIGHT_M, MAX_HEIGHT_M)
        _require_range("mic_height_m", self.mic_height_m, MIN_HEIGHT_M, MAX_HEIGHT_M)
        _require_range("distance_m", self.distance_m, MIN_DISTANCE_M, MAX_DISTANCE_M)
        for name in _LEGACY_WALL_FIELDS.values():
            wall_m = getattr(self, name)
            if wall_m is not None:
                _require_range(name, wall_m, MIN_WALL_M, MAX_WALL_M)
        if self.cabinet_back_wall_m is not None:
            _require_range("cabinet_back_wall_m", self.cabinet_back_wall_m, 0.0, MAX_WALL_M)
        if self.cabinet_back_wall_m is not None and self.front_wall_m is not None:
            raise GeometryFieldError("front_wall_m", "declare cabinet-back gap or legacy baffle distance, not both")
        if self.cabinet_depth_m is not None:
            _require_range("cabinet_depth_m", self.cabinet_depth_m, 0.0, math.inf)
            if self.cabinet_depth_m == 0.0:
                raise GeometryFieldError("cabinet_depth_m", "cabinet depth must be positive")
        if self.toe_in_degrees is not None:
            _require_range("toe_in_degrees", self.toe_in_degrees, -90.0, 90.0, "degrees")
        if self.ceiling_height_m is None:
            return
        _require_range("ceiling_height_m", self.ceiling_height_m, MIN_HEIGHT_M, MAX_CEILING_M)
        if not (
            self.ceiling_height_m > self.speaker_height_m
            and self.ceiling_height_m > self.mic_height_m
        ):
            raise GeometryFieldError(
                "ceiling_height_m",
                f"ceiling_height_m ({self.ceiling_height_m:g}) must be greater than "
                f"both speaker_height_m ({self.speaker_height_m:g}) and "
                f"mic_height_m ({self.mic_height_m:g})",
            )

    def first_bounce_s(self, distance_m: float | None = None) -> float:
        """Excess time-of-arrival of the earliest room reflection, over direct.

        ``distance_m`` evaluates this rig at ONE capture's own speaker-to-mic
        distance; ``None`` alone falls back to the declared :attr:`distance_m`,
        while a non-finite or non-positive override raises
        :class:`GeometryFieldError`. The two heights are the DECLARED ones at
        every elevation -- nothing in a round measures where the capsule
        actually ended up.
        """
        if distance_m is None:
            distance = self.distance_m
        else:
            distance = float(distance_m)
            if not math.isfinite(distance) or distance <= 0.0:
                raise GeometryFieldError(
                    "distance_m",
                    f"distance_m must be a positive finite length (got {distance_m!r})",
                )
        direct_m = math.hypot(distance, self.speaker_height_m - self.mic_height_m)
        bounce_paths_m = [
            math.hypot(distance, self.speaker_height_m + self.mic_height_m),
        ]
        if self.ceiling_height_m is not None:
            bounce_paths_m.append(
                math.hypot(
                    distance,
                    (self.ceiling_height_m - self.speaker_height_m)
                    + (self.ceiling_height_m - self.mic_height_m),
                )
            )
        return (min(bounce_paths_m) - direct_m) / DEFAULT_SOUND_SPEED_M_S

    def entanglement_floor_hz(self, distance_m: float | None = None) -> float:
        """This rig's entanglement floor, from :meth:`first_bounce_s`."""
        return f_entanglement_floor_hz(self.first_bounce_s(distance_m))

    def boundary_walls(self) -> tuple[dict[str, float], str]:
        """Baffle-reference distances for the advisory prior, never DSP delay.

        The derived front distance is to the front-panel centre, not an assumed
        acoustic centre for every driver. A directivity fit needs source geometry.
        """
        walls = {key: distance for key, name in _LEGACY_WALL_FIELDS.items()
                 if (distance := getattr(self, name)) is not None}
        if self.cabinet_back_wall_m is not None:
            if self.cabinet_depth_m is None or self.toe_in_degrees is None:
                return walls, "front_baffle_geometry_undeclared"
            walls["front"] = self.cabinet_back_wall_m + self.cabinet_depth_m * math.cos(
                math.radians(self.toe_in_degrees)
            )
        return walls, "" if walls else "walls_undeclared"

    def to_dict(self) -> dict[str, float]:
        """The banked shape. An undeclared field is ABSENT, never null."""
        return {
            name: value
            for name, value in asdict(self).items()
            if value is not None
        }

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> "DeclaredGeometry":
        """:meth:`to_dict`'s inverse, through the constructor's own refusals."""
        values: dict[str, Any] = {
            name: doc.get(name)
            for name in ("speaker_height_m", "mic_height_m", "distance_m")
        }
        values.update({field.name: doc[field.name] for field in fields(cls)
                       if doc.get(field.name) is not None})
        return cls(**values)

    def save(self, path: str | Path = DEFAULT_PATH) -> None:
        atomic_write_json(
            Path(path), {**self.to_dict(), "source": ENTANGLEMENT_SOURCE_DECLARED}
        )

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PATH) -> "DeclaredGeometry":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data if isinstance(data, Mapping) else {})


def boundary_prior(
    freqs_hz: Iterable[float], *, walls: Mapping[str, float]
) -> dict[str, Any]:
    """Advisory boundary gain over ``freqs_hz`` for the declared walls.

    Per wall at distance ``d`` the reflected path exceeds the direct one by
    ``2d``; the rigid image-source pressure sum ``|1 + exp(-j 2*pi*f * 2d/c)|``
    is +6 dB as ``f -> 0`` (2*pi loading) and nulls at ``c / (4 d)``.
    ``f_half_gain_hz`` is where the rise is at +3 dB.

    Summing the per-wall dB curves IS the rigid corner image-source sum, not
    an independence approximation: adding logs multiplies the magnitudes, and
    ``(1 + exp(-j th_front))(1 + exp(-j th_side))`` expands to exactly the
    four sources -- direct, one image per wall, and the corner image. What it
    does approximate is equal image amplitudes (no ``1/r`` spreading loss) and
    perpendicular rigid walls; each wall's curve is then clamped at
    :data:`BOUNDARY_PRIOR_NULL_FLOOR_DB`, which the product no longer models.

    No declared wall is not a flat 0 dB claim, it is no claim: the returned
    grid and curve are then empty.
    """
    speed = DEFAULT_SOUND_SPEED_M_S
    grid = [float(hz) for hz in freqs_hz]
    declared: dict[str, dict[str, float]] = {}
    curves: list[list[float]] = []
    for key, distance in walls.items():
        _require_range(_LEGACY_WALL_FIELDS[key], distance, MIN_WALL_M, math.inf)
        metres = float(distance)
        declared[key] = {
            "distance_m": metres,
            "f_null_hz": speed / (4.0 * metres),
            "f_half_gain_hz": speed / (8.0 * metres),
        }
        curves.append([
            20.0 * math.log10(max(
                abs(2.0 * math.cos(2.0 * math.pi * float(hz) * metres / speed)),
                _NULL_FLOOR_MAGNITUDE,
            ))
            for hz in grid
        ])
    return {
        "sound_speed_m_s": speed,
        "sound_speed_source": "default",
        "walls": declared,
        "freqs_hz": grid if curves else [],
        "prior_db": [sum(wall_db) for wall_db in zip(*curves)],
    }


def load_declared_geometry(path: str | Path = DEFAULT_PATH) -> DeclaredGeometry | None:
    """The declared rig, or ``None`` when the operator has declared none.

    Nothing declared is the ORDINARY state; a file that EXISTS and does not
    parse raises instead. Wizard-owned and rewritten from a separate process,
    so a long-lived daemon must never cache what it returns.
    """
    try:
        return DeclaredGeometry.load(path)
    except FileNotFoundError:
        return None


def declared_first_bounce_s(
    distance_m: float | None = None, *, path: str | Path = DEFAULT_PATH
) -> float | None:
    """The declared rig's first bounce at ONE capture's distance, in seconds."""
    geometry = load_declared_geometry(path)
    return None if geometry is None else geometry.first_bounce_s(distance_m)
