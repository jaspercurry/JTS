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
single writer) stores :class:`DeclaredGeometry` to :data:`DEFAULT_PATH`, and
the gate disclosures ask that file where it lives, at the point of use.
Banking a round copies it beside the bundle as ``declared-geometry.json``
(``jasper-round-bank`` and ``scripts/bank-crossover-round.sh``, on the terms
:mod:`jasper.active_speaker.crossover_v2.round_inputs` states); the evidence
packet builder reads only the path its caller resolved, never this one.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

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


class GeometryFieldError(ValueError):
    """A refusal that names the offending field as data, not only as prose."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def _metres(name: str, value: object) -> float:
    """One declared length as a number of metres, refused BY NAME otherwise.

    ``bool`` is excluded deliberately: it is an ``int``, so a JSON ``true``
    would otherwise read as a metre.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GeometryFieldError(
            name, f"{name} must be a number of metres (got {value!r})"
        )
    return float(value)


def _require_range(name: str, value: object, lo: float, hi: float) -> None:
    # NaN and the infinities fail every comparison, so the range check refuses
    # them too and no separate finiteness rule is needed.
    metres = _metres(name, value)
    if not (lo <= metres <= hi):
        raise GeometryFieldError(
            name, f"{name} must be within [{lo:g}, {hi:g}] m (got {metres:g})"
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

    def first_bounce_s(self, distance_m: float | None = None) -> float:
        """Excess time-of-arrival of the earliest room reflection, over direct.

        The floor-bounce path always participates; the ceiling-bounce path
        joins the minimum only when :attr:`ceiling_height_m` was declared --
        a low ceiling can then win over the floor.

        ``distance_m`` evaluates this rig at ONE capture's own
        speaker-to-mic distance without mutating the record. The two heights
        are rig facts and stay declared once; the distance is the capture's
        (``PositionGeometry.mark_distance_m``), and the floor rises as the
        microphone moves OUT: closing in lengthens the bounce path against a
        direct path that shortens faster, so the excess arrival time grows and
        the floor falls -- monotonically, for every pair of heights, which is
        the same physics a near-field capture buys its low-end validity with.
        ONLY ``None`` means "this capture states no distance" and falls back to
        the declared :attr:`distance_m`; a non-finite or non-positive override
        is a caller stating a distance that is not one, and raises
        :class:`GeometryFieldError` rather than silently reporting the rig's
        floor under the capture's name.

        **Every production pose declares the same distance TODAY.** The one
        caller is ``PositionGeometry.mark_distance_m``, and that is
        ``spatial.MARK_DISTANCE_M`` (1.0 m) on every pose a round walks -- the
        mark is a reference length, not a surveyed capsule distance -- so every
        seat of a round currently derives the SAME floor and pooling them
        cannot yet disagree. The per-capture parameter is what lets a row that
        carries its own distance (#3498) be evaluated at it without this
        method or any consumer changing.

        The two heights are the DECLARED ones at every elevation: a pose raised
        or lowered off the mark (``PositionGeometry.vertical_deg``) is timed
        against the declared mic height unadjusted, because nothing in a round
        measures where the capsule actually ended up.
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
        """This rig's entanglement floor, from :meth:`first_bounce_s`.

        The floor itself is
        :func:`jasper.audio_measurement.gating.f_entanglement_floor_hz` --
        imported from there, never duplicated. ``distance_m`` reaches
        :meth:`first_bounce_s` unchanged.
        """
        return f_entanglement_floor_hz(self.first_bounce_s(distance_m))

    def to_dict(self) -> dict[str, float]:
        """The banked shape. An undeclared ceiling is ABSENT, never null."""
        return {
            name: value
            for name, value in asdict(self).items()
            if value is not None
        }

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> "DeclaredGeometry":
        """:meth:`to_dict`'s inverse, through the constructor's own refusals.

        A missing distance arrives as ``None`` and is refused by name, so a
        half-written record cannot read as a room.
        """
        fields: dict[str, Any] = {
            name: doc.get(name)
            for name in ("speaker_height_m", "mic_height_m", "distance_m")
        }
        if doc.get("ceiling_height_m") is not None:
            fields["ceiling_height_m"] = doc["ceiling_height_m"]
        return cls(**fields)

    def save(self, path: str | Path = DEFAULT_PATH) -> None:
        atomic_write_json(
            Path(path), {**self.to_dict(), "source": ENTANGLEMENT_SOURCE_DECLARED}
        )

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PATH) -> "DeclaredGeometry":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        # A document that is not an object refuses as a geometry missing every
        # field, rather than as an AttributeError from inside ``from_dict``.
        return cls.from_dict(data if isinstance(data, Mapping) else {})


def load_declared_geometry(path: str | Path = DEFAULT_PATH) -> DeclaredGeometry | None:
    """The declared rig, or ``None`` when the operator has declared none.

    Nothing declared is the ORDINARY state: every consumer then publishes
    ``entanglement_floor_source = unknown``, warns about nothing, and refuses
    nothing. A file that EXISTS and does not parse is the other fact
    entirely -- a defect in the single writer or a hand-edit -- and raises,
    because reading it as "absent" would hide it forever.

    Read at the point of use. This file is wizard-owned and
    ``jasper-declare-geometry`` rewrites it from a separate process, so a
    long-lived daemon must never cache what it returns.
    """
    try:
        return DeclaredGeometry.load(path)
    except FileNotFoundError:
        return None


def declared_first_bounce_s(
    distance_m: float | None = None, *, path: str | Path = DEFAULT_PATH
) -> float | None:
    """The declared rig's first bounce at ONE capture's distance, in seconds.

    :func:`load_declared_geometry` then
    :meth:`DeclaredGeometry.first_bounce_s` -- the composition every gate
    disclosure wants, in one place so no caller pairs a load with its own
    arithmetic. ``None`` when nothing is declared, which is exactly what
    :func:`jasper.audio_measurement.gate_disclosure.build_gate_disclosure`
    reads as ``unknown``.
    """
    geometry = load_declared_geometry(path)
    return None if geometry is None else geometry.first_bounce_s(distance_m)
