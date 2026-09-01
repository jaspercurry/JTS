# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator-declared measurement rig geometry.

:mod:`jasper.audio_measurement.gating`'s reflection finder requires the
smoothed envelope to drop 12 dB below the direct peak and re-cross with
7.5 dB prominence. On JTS's rig class the geometric first bounce arrives
only ~2.5-3 ms after the direct sound, while the direct sound is still
decaying -- so the measured source structurally never fires and
``entanglement_floor_hz`` would publish UNKNOWN forever. This module lets
the operator declare the rig's geometry once so the floor can instead be
DERIVED and disclosed with ``declared_geometry`` provenance, never
mistaken for a measurement. See issue #3502.

``jasper-declare-geometry`` (:mod:`jasper.cli.declare_geometry`, the
single writer) stores :class:`DeclaredGeometry` to
:data:`DEFAULT_PATH`.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

from .gating import ENTANGLEMENT_SOURCE_DECLARED, f_entanglement_floor_hz
from .null_walk import DEFAULT_SOUND_SPEED_M_S
from ..atomic_io import atomic_write_json

DEFAULT_PATH = "/var/lib/jasper/measurement_geometry.json"

MIN_HEIGHT_M = 0.1
MAX_HEIGHT_M = 3.0
MIN_DISTANCE_M = 0.15
MAX_DISTANCE_M = 3.0
MAX_CEILING_M = 6.0


class GeometryFieldError(ValueError):
    """A refusal that names the offending field as data, not only as prose."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def _require_range(name: str, value: float, lo: float, hi: float) -> None:
    if not (lo <= value <= hi):
        raise GeometryFieldError(
            name, f"{name} must be within [{lo:g}, {hi:g}] m (got {value:g})"
        )


@dataclass(frozen=True)
class DeclaredGeometry:
    """Operator-declared rig geometry: two heights, a distance, an optional ceiling.

    ``ceiling_height_m`` is ``None`` when no ceiling was declared -- the
    ceiling-bounce family is then simply absent from
    :meth:`first_bounce_s`'s minimum rather than refused.
    """

    speaker_height_m: float
    mic_height_m: float
    distance_m: float
    ceiling_height_m: float | None = None

    def __post_init__(self) -> None:
        _require_range("speaker_height_m", self.speaker_height_m, MIN_HEIGHT_M, MAX_HEIGHT_M)
        _require_range("mic_height_m", self.mic_height_m, MIN_HEIGHT_M, MAX_HEIGHT_M)
        _require_range("distance_m", self.distance_m, MIN_DISTANCE_M, MAX_DISTANCE_M)
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

    def first_bounce_s(self) -> float:
        """Excess time-of-arrival of the earliest room reflection, over direct.

        The floor-bounce path always participates; the ceiling-bounce path
        joins the minimum only when :attr:`ceiling_height_m` was declared --
        a low ceiling can then win over the floor.
        """
        direct_m = math.hypot(self.distance_m, self.speaker_height_m - self.mic_height_m)
        bounce_paths_m = [
            math.hypot(self.distance_m, self.speaker_height_m + self.mic_height_m),
        ]
        if self.ceiling_height_m is not None:
            bounce_paths_m.append(
                math.hypot(
                    self.distance_m,
                    (self.ceiling_height_m - self.speaker_height_m)
                    + (self.ceiling_height_m - self.mic_height_m),
                )
            )
        return (min(bounce_paths_m) - direct_m) / DEFAULT_SOUND_SPEED_M_S

    def entanglement_floor_hz(self) -> float:
        """This rig's entanglement floor, from :meth:`first_bounce_s`.

        The floor itself is
        :func:`jasper.audio_measurement.gating.f_entanglement_floor_hz` --
        imported from there, never duplicated.
        """
        return f_entanglement_floor_hz(self.first_bounce_s())

    def save(self, path: str | Path = DEFAULT_PATH) -> None:
        atomic_write_json(
            Path(path), {**asdict(self), "source": ENTANGLEMENT_SOURCE_DECLARED}
        )

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PATH) -> "DeclaredGeometry":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        ceiling = data.get("ceiling_height_m")
        return cls(
            speaker_height_m=float(data["speaker_height_m"]),
            mic_height_m=float(data["mic_height_m"]),
            distance_m=float(data["distance_m"]),
            ceiling_height_m=float(ceiling) if ceiling is not None else None,
        )
