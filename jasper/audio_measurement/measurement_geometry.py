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

from .gating import TRUSTED_FLOOR_MULTIPLIER
from ..atomic_io import atomic_write_text

DEFAULT_PATH = "/var/lib/jasper/measurement_geometry.json"

#: Written into the JSON alongside the geometry fields. Issue #3502 amends
#: the gate-disclosure provenance vocabulary to carry this value once a
#: later PR wires this module into that flow; this module does not import
#: or touch gate_disclosure.py itself.
PROVENANCE = "declared_geometry"

# Speed of sound in air at 20 degC (nominal room temperature). First-bounce
# timing is not sensitive enough to the ~1%/10 degC seasonal swing to
# warrant a measured value here.
SPEED_OF_SOUND_M_S = 343.0

MIN_HEIGHT_M = 0.1
MAX_HEIGHT_M = 3.0
MIN_DISTANCE_M = 0.15
MAX_DISTANCE_M = 3.0


def _require_range(name: str, value: float, lo: float, hi: float) -> None:
    if not (lo <= value <= hi):
        raise ValueError(f"{name} must be within [{lo:g}, {hi:g}] m (got {value:g})")


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
        if self.ceiling_height_m is not None and not (
            self.ceiling_height_m > self.speaker_height_m
            and self.ceiling_height_m > self.mic_height_m
        ):
            raise ValueError(
                f"ceiling_height_m ({self.ceiling_height_m:g}) must be greater than "
                f"both speaker_height_m ({self.speaker_height_m:g}) and "
                f"mic_height_m ({self.mic_height_m:g})"
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
        return (min(bounce_paths_m) - direct_m) / SPEED_OF_SOUND_M_S

    def entanglement_floor_hz(self) -> float:
        """The trusted-floor multiplier applied to :meth:`first_bounce_s`.

        Same 2.5 constant as :func:`jasper.audio_measurement.gating.f_trusted_floor_hz`
        -- imported from there, never duplicated.
        """
        return TRUSTED_FLOOR_MULTIPLIER / self.first_bounce_s()

    def save(self, path: str | Path = DEFAULT_PATH) -> None:
        payload = {**asdict(self), "source": PROVENANCE}
        atomic_write_text(Path(path), json.dumps(payload, indent=2) + "\n")

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
