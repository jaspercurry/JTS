# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Named measurement programs loaded from the bundled measurement plan."""

from __future__ import annotations

import json
import math
import numbers
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from jasper.audio_measurement.gating import SEAT_EXEMPT

POSE_KIND_BEARING = "bearing"
POSE_KIND_SEAT = "seat"
POSE_KIND_CLOSE = "close"
POSE_KINDS = (POSE_KIND_BEARING, POSE_KIND_SEAT, POSE_KIND_CLOSE)

PURPOSE_SPEAKER = "speaker"
PURPOSE_ROOM = "room"
PURPOSE_BASS = "bass"
PURPOSE_REFERENCE = "reference"
PURPOSES = (PURPOSE_SPEAKER, PURPOSE_ROOM, PURPOSE_BASS, PURPOSE_REFERENCE)

REGIME_PER_DRIVER = "per_driver"
REGIME_SUMMED = "summed"
REGIME_BRANCHES = "branches"
REGIMES = (REGIME_PER_DRIVER, REGIME_SUMMED, REGIME_BRANCHES)

_LEGACY_PURPOSE_BY_KIND = {
    POSE_KIND_BEARING: PURPOSE_SPEAKER,
    POSE_KIND_SEAT: PURPOSE_ROOM,
    POSE_KIND_CLOSE: PURPOSE_REFERENCE,
}


def _validated_purpose(purpose: str | None) -> str:
    if purpose is None:
        return PURPOSE_SPEAKER
    if purpose not in PURPOSES:
        raise ValueError(f"a measurement purpose must be one of {PURPOSES}, got {purpose!r}")
    return purpose


def resolved_measurement_purpose(purpose: str | None, kind: str) -> str:
    """Resolve explicit purpose, or infer the purpose of an old pose."""

    if purpose is not None:
        return _validated_purpose(purpose)
    try:
        return _LEGACY_PURPOSE_BY_KIND[kind]
    except KeyError:
        raise ValueError(f"a pose kind must be one of {POSE_KINDS}, got {kind!r}") from None


def validated_capture_purpose(purpose: str | None, kind: str, regime: str) -> str:

    """Resolve purpose and validate the capture mode supported by the runner."""
    resolved = resolved_measurement_purpose(purpose, kind)
    if regime not in REGIMES:
        raise ValueError(f"a measurement regime must be one of {REGIMES}, got {regime!r}")
    if resolved != PURPOSE_SPEAKER and regime != REGIME_SUMMED:
        raise ValueError(f"{resolved} measurements require the summed regime")
    return resolved


def bookkeeping_views(program: str) -> tuple[str, ...]:
    return {
        PURPOSE_SPEAKER: ("inventory", "classify-features", "distortion", "directivity", "frozen", "per-seat"),
        PURPOSE_ROOM: ("room", "room-grade"),
        PURPOSE_BASS: ("bass", "bass-compare"),
    }.get(program, ())


def baseline_scope(purpose: str | None) -> str:
    if _validated_purpose(purpose) == PURPOSE_BASS:
        return "room_tune"
    return (
        "speaker_tune"
        if _validated_purpose(purpose) in (PURPOSE_ROOM, PURPOSE_REFERENCE)
        else "base"
    )


def gate_exemption(purpose: str | None) -> str | None:
    return SEAT_EXEMPT if _validated_purpose(purpose) in (PURPOSE_ROOM, PURPOSE_BASS) else None


def validated_pose(
    kind: str,
    seat_offset_m: Sequence[float] | None,
    distance_m: float | None = None,
) -> tuple[tuple[float, float, float] | None, float | None]:
    """The one rule every carrier of a pose category checks: ``kind`` is one
    of :data:`POSE_KINDS`; exactly a seat states three finite metres
    ``(right, forward, up)`` from the head; a distance, when stated, is a
    positive length. Returns the offset and distance normalized to floats;
    raises ``ValueError``."""

    if kind not in POSE_KINDS:
        raise ValueError(f"a pose kind must be one of {POSE_KINDS}, got {kind!r}")
    if (seat_offset_m is not None) != (kind == POSE_KIND_SEAT):
        raise ValueError(
            "a seat pose states its (right, forward, up) offset from the head; "
            "no other kind does"
        )
    offset = None
    if seat_offset_m is not None:
        try:
            offset = tuple(float(v) for v in seat_offset_m)
        except (TypeError, ValueError):
            offset = ()
        if len(offset) != 3 or not all(math.isfinite(v) for v in offset):
            raise ValueError(f"a seat offset is three finite metres, got {seat_offset_m!r}")
    distance = None
    if distance_m is not None:
        distance = float(distance_m) if isinstance(distance_m, (int, float)) else math.nan
        if not math.isfinite(distance) or distance <= 0:
            raise ValueError(f"a pose distance is a positive length in metres, got {distance_m!r}")
    return offset, distance  # type: ignore[return-value]


def pose_place(
    kind: str,
    azimuth_deg: int,
    elevation_deg: int,
    distance_m: float | None,
    seat_offset_m: tuple[float, float, float] | None,
) -> tuple[object, ...]:
    """What distinguishes one microphone position from another."""

    return (kind, azimuth_deg, elevation_deg, distance_m, seat_offset_m)


@dataclass(frozen=True)
class ProgramPose:
    """One place to measure, its take count, and optional prompt text."""

    azimuth_deg: int
    elevation_deg: int
    repeats: int = 1
    kind: str = POSE_KIND_BEARING
    distance_m: float | None = None
    seat_offset_m: tuple[float, float, float] | None = None
    headline: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "azimuth_deg", validated_angle(self.azimuth_deg))
        object.__setattr__(self, "elevation_deg", validated_angle(self.elevation_deg))
        if isinstance(self.repeats, bool) or not isinstance(self.repeats, int) or self.repeats <= 0:
            raise ValueError(f"pose repeats must be a positive integer, got {self.repeats!r}")
        if not isinstance(self.headline, str) or not isinstance(self.detail, str):
            raise ValueError("pose headline and detail must be text")
        offset, distance = validated_pose(self.kind, self.seat_offset_m, self.distance_m)
        object.__setattr__(self, "seat_offset_m", offset)
        object.__setattr__(self, "distance_m", distance)

    @property
    def place(self) -> tuple[object, ...]:
        return pose_place(
            self.kind, self.azimuth_deg, self.elevation_deg,
            self.distance_m, self.seat_offset_m,
        )


@dataclass(frozen=True)
class MeasurementProgram:
    """One named menu item: an ordered pose list and capture purpose."""

    program_id: str
    size: str
    poses: tuple[ProgramPose, ...]
    purpose: str = PURPOSE_SPEAKER
    regime: str = REGIME_PER_DRIVER
    mover: str | None = None

    def __post_init__(self) -> None:
        if not self.poses:
            raise ValueError("a measurement program must contain at least one pose")
        validated_capture_purpose(self.purpose, POSE_KIND_BEARING, self.regime)

    @property
    def mic_move_count(self) -> int:
        """Distinct places — repeats stay at one place and move nothing."""

        return len({p.place for p in self.poses})

    @property
    def capture_count(self) -> int:
        return sum(p.repeats for p in self.poses)


class UnknownProgramError(ValueError):
    """No such ``(program_id, size)``. ``choices`` carries the valid pairs."""

    def __init__(
        self,
        program_id: str,
        size: str | None,
        choices: tuple[tuple[str, str], ...],
    ) -> None:
        self.program_id = program_id
        self.size = size
        self.choices = choices
        offered = ", ".join(f"{pid}/{sz}" for pid, sz in choices) or "(none)"
        requested = size if size is not None else "<default>"
        super().__init__(f"no measurement program {program_id}/{requested}; choose one of: {offered}")


def validated_angle(value: object) -> int:
    """Normalize a whole-degree bearing without imposing mover reach."""

    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        raise ValueError(f"an angle must be stated in whole degrees, got {value!r}")
    return int(value)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be nonempty text")
    return value


def _pose(value: Any, layout: str, index: int) -> ProgramPose:
    label = f"layout {layout!r} pose {index}"
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    try:
        return ProgramPose(**value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}: {exc}") from None


def _config_text(path: str | Path | None) -> str:
    if path is not None:
        return Path(path).read_text(encoding="utf-8")
    return resources.files(__package__).joinpath("measurement_plans.json").read_text(encoding="utf-8")


def _load_programs(
    path: str | Path | None = None,
) -> tuple[Mapping[tuple[str, str], MeasurementProgram], Mapping[str, str]]:
    raw = json.loads(_config_text(path))
    if not isinstance(raw, dict):
        raise ValueError("measurement plan must be an object")
    unknown = set(raw) - {"layouts", "programs", "default_sizes"}
    if unknown:
        raise ValueError(f"measurement plan has unknown fields: {sorted(unknown)}")

    layouts_raw = raw.get("layouts")
    if not isinstance(layouts_raw, dict) or not layouts_raw:
        raise ValueError("measurement plan layouts must be a nonempty object")
    layouts: dict[str, tuple[ProgramPose, ...]] = {}
    movers: dict[str, str] = {}
    for name, values in layouts_raw.items():
        name = _text(name, "layout name")
        if isinstance(values, dict):
            unknown = set(values) - {"poses", "mover"}
            if unknown:
                raise ValueError(f"layout {name!r} has unknown fields: {sorted(unknown)}")
            if "mover" in values:
                movers[name] = _text(values["mover"], f"layout {name!r} mover")
            values = values.get("poses")
        if not isinstance(values, list) or not values:
            raise ValueError(f"layout {name!r} must contain at least one pose")
        layouts[name] = tuple(_pose(value, name, index) for index, value in enumerate(values))

    rows = raw.get("programs")
    if not isinstance(rows, list) or not rows:
        raise ValueError("measurement plan programs must be a nonempty list")
    programs: dict[tuple[str, str], MeasurementProgram] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"program {index} must be an object")
        unknown = set(row) - {"id", "size", "layout", "purpose", "regime"}
        if unknown:
            raise ValueError(f"program {index} has unknown fields: {sorted(unknown)}")
        try:
            program_id = _text(row["id"], f"program {index} id")
            size = _text(row["size"], f"program {index} size")
            layout = _text(row["layout"], f"program {index} layout")
        except KeyError as exc:
            raise ValueError(f"program {index} is missing {exc.args[0]}") from None
        if layout not in layouts:
            raise ValueError(f"program {program_id}/{size} names unknown layout {layout!r}")
        key = (program_id, size)
        if key in programs:
            raise ValueError(f"measurement plan repeats program {program_id}/{size}")
        programs[key] = MeasurementProgram(
            program_id,
            size,
            layouts[layout],
            purpose=row.get("purpose", PURPOSE_SPEAKER),
            regime=row.get("regime", REGIME_PER_DRIVER),
            mover=movers.get(layout),
        )

    defaults_raw = raw.get("default_sizes")
    if not isinstance(defaults_raw, dict):
        raise ValueError("measurement plan default_sizes must be an object")
    program_ids = {program_id for program_id, _size in programs}
    if set(defaults_raw) != program_ids:
        raise ValueError("measurement plan must name one default size for every program id")
    defaults: dict[str, str] = {}
    for program_id, size in defaults_raw.items():
        program_id = _text(program_id, "default program id")
        size = _text(size, f"default size for {program_id}")
        if (program_id, size) not in programs:
            raise ValueError(f"default {program_id}/{size} is not a program")
        defaults[program_id] = size
    return MappingProxyType(programs), MappingProxyType(defaults)

def load_programs(
    path: str | Path | None = None,
) -> Mapping[tuple[str, str], MeasurementProgram]:
    """Load and validate the bundled plan, or a plan at ``path``."""

    return _load_programs(path)[0]


_PROGRAMS, _DEFAULT_SIZES = _load_programs()

# Compatibility values derived from the config, which remains their owner.
ANCHOR_REPEATS = _PROGRAMS[("baseline", "full")].poses[0].repeats
SEAT_OFFSET_M = max(
    abs(component)
    for pose in _PROGRAMS[("seat", "cloud")].poses
    for component in pose.seat_offset_m or ()
)
CLOSE_DISTANCE_M = _PROGRAMS[("close", "spot")].poses[0].distance_m


def available_programs() -> tuple[tuple[str, str], ...]:
    """The ``(program_id, size)`` pairs a menu may offer, sorted.

    ``spot`` is absent on purpose: it carries caller geometry, so it is reached
    through :func:`spot_program` rather than looked up by name.
    """

    return tuple(sorted(_PROGRAMS))


def program(program_id: str, size: str | None = None) -> MeasurementProgram:
    """Return a named program, using its configured size when omitted."""

    requested_size = size
    if size is None:
        size = _DEFAULT_SIZES.get(program_id)
    try:
        return _PROGRAMS[(program_id, size)]  # type: ignore[index]
    except KeyError:
        raise UnknownProgramError(program_id, requested_size, available_programs()) from None


def spot_program(azimuth_deg: int, elevation_deg: int) -> MeasurementProgram:
    """One take at one caller-supplied bearing."""

    return MeasurementProgram(
        "spot", "express", (ProgramPose(azimuth_deg, elevation_deg),)
    )
