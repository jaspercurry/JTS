# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The tuning programs, and the measurement presets loaded from the bundled measurement plan."""

from __future__ import annotations

import json
import math
import numbers
from dataclasses import dataclass, replace
from itertools import groupby
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Any, Collection, Mapping, Sequence

from jasper.audio_measurement.piston import at_driver_near_field
from jasper.audio_routes.output_topology import OutputTopology, topology_is_subless_passive_mains
from jasper.platform.speaker_layout import cardioid_cabinet_channels, measurement_target_id, measurement_target_parts

from .measurement import active_driver_targets

POSE_KIND_BEARING = "bearing"
POSE_KIND_SEAT = "seat"
POSE_KIND_CLOSE = "close"
#: A pose behind the cabinet, on axis, ``distance_m`` from the back panel
#: toward the wall -- the cardioid null the turntable arm cannot reach
#: (issue #5330).
POSE_KIND_BEHIND = "behind"
POSE_KINDS = (POSE_KIND_BEARING, POSE_KIND_SEAT, POSE_KIND_CLOSE, POSE_KIND_BEHIND)

PURPOSE_SPEAKER = "speaker"
PURPOSE_ROOM = "room"
PURPOSE_BASS = "bass"
PURPOSE_REFERENCE = "reference"
PURPOSE_REAR = "rear"

REGIME_PER_DRIVER = "per_driver"
REGIME_SUMMED = "summed"
REGIME_BRANCHES = "branches"
REGIME_NEAR_FIELD = "near_field"
REGIMES = (REGIME_PER_DRIVER, REGIME_SUMMED, REGIME_BRANCHES, REGIME_NEAR_FIELD)


@dataclass(frozen=True)
class PrescriptionSection:
    name: str
    kind: str | None
    document_order: int
    judge_order: int
    reset: bool = True
    compose: bool = True
    #: The keys the judge adds to an authored section before its reader sees it: ``kind``
    #: from this row, ``artifact_schema_version`` from the section's contract, ``rationale``
    #: from the document. Each reader refuses a key it does not name.
    envelope: tuple[str, ...] = ()


_VERSIONED = ("kind", "artifact_schema_version")


@dataclass(frozen=True)
class CandidateField:
    name: str
    type: type
    snapshot: bool = True


@dataclass(frozen=True)
class TuningProgram:
    purpose: str
    sections: tuple[PrescriptionSection, ...]
    candidate_fields: tuple[CandidateField, ...]
    regimes: tuple[str, ...]
    purpose_order: int
    title: str
    description: str
    measure_label: str
    applied_name: str
    #: The ``(preset, layout)`` a trial of this program's documents may walk; the first is the default.
    trial: tuple[tuple[str, str], ...]
    preview: tuple[int, str, tuple[str, ...]] | None = None
    profile_fallback: bool = True
    graph_evidence: bool = False
    #: Applied layers every take of this purpose plays cleared, and whether its
    #: base, or its branches take, also clears the purpose's own layer
    #: (doctrine §1a; ADR-0370, ADR-0386).
    clears: tuple[str, ...] = ()
    base_clears_own: bool = False
    branches_clear_own: bool = False


# Row order is the tuning order; stored documents retain their existing orders.
_PROGRAM_SECTIONS = (
    TuningProgram(
        PURPOSE_SPEAKER,
        (PrescriptionSection("driver", "jts_crossover_driver_prescription", 0, 6, envelope=(*_VERSIONED, "rationale")),
         PrescriptionSection("blend", "jts_crossover_blend_prescription", 1, 1, envelope=(*_VERSIONED, "rationale")),
         PrescriptionSection("alignment", "jts_crossover_alignment_prescription", 2, 2, compose=False, envelope=_VERSIONED),
         PrescriptionSection("topology", "jts_crossover_topology_prescription", 3, 0, reset=False, envelope=_VERSIONED)),
        (CandidateField("linearization", dict), CandidateField("linearization_outcome", str, False),
         CandidateField("trim_decision", dict, False), CandidateField("exclusion_evidence", dict, False),
         CandidateField("blend_correction", list)),
        (REGIME_PER_DRIVER, REGIME_SUMMED, REGIME_BRANCHES), 0,
        "Driver linearization and crossover", "Measure each driver and refine its response and crossover.",
        "Measure the baseline", "driver", trial=(("speaker/mark", "speaker_mark"),), preview=(2, "emitted_graph", ("driver", "blend", "topology")),
    ),
    TuningProgram(
        PURPOSE_REAR, (PrescriptionSection("rear_calibration", "jts_rear_calibration", 6, 5),),
        (CandidateField("rear_calibration", dict),), (REGIME_SUMMED, REGIME_BRANCHES), 4,
        "Cardioid tuning", "Set the rear woofer to reduce sound behind the speaker.",
        "Measure the rear woofer", "rear", trial=(("rear/seat", "seat_express"), ("rear/express", "rear_express")),
        preview=(0, "rear_calibration", ("rear_calibration",)), profile_fallback=False, graph_evidence=True,
        branches_clear_own=True,
    ),
    TuningProgram(
        PURPOSE_BASS, (PrescriptionSection("bass", None, 5, 4),),
        (CandidateField("bass_extension", dict),), (REGIME_SUMMED,), 2,
        "Bass extension", "Extend low bass within the driver's limits.", "Measure bass", "bass",
        trial=(("bass/axis", "bass_axis"), ("bass/axis", "seat_express")), graph_evidence=True,
        clears=("room_correction",), base_clears_own=True,
    ),
    TuningProgram(
        PURPOSE_ROOM, (PrescriptionSection("room", "jts_room_prescription", 4, 3, envelope=(*_VERSIONED, "rationale")),),
        (CandidateField("room_correction", dict),), (REGIME_SUMMED,), 1,
        "Room correction", "Adjust the sound at your listening position.", "Measure the room", "room",
        trial=(("room/seat", "seat_express"), ("room/seat", "room_quick")), preview=(1, "room", ("room",)),
    ),
)
PROGRAM_ROWS = _PROGRAM_SECTIONS
PROGRAM_DOCUMENT_ORDER = tuple(sorted(_PROGRAM_SECTIONS, key=lambda row: row.purpose_order))
PRESCRIPTION_SECTIONS = tuple(sorted(
    (section for row in _PROGRAM_SECTIONS for section in row.sections), key=lambda section: section.document_order,
))
PURPOSES = tuple(name for _, name in sorted(
    [(row.purpose_order, row.purpose) for row in _PROGRAM_SECTIONS] + [(3, PURPOSE_REFERENCE)],
))
#: Tuning order.
RUNNABLE_PROGRAMS = tuple(row.purpose for row in _PROGRAM_SECTIONS)
PROGRAM_DETAILS = {row.purpose: {"title": row.title, "description": row.description} for row in _PROGRAM_SECTIONS}
PROGRAM_ENTRIES = tuple({"id": name, **PROGRAM_DETAILS[name]} for name in RUNNABLE_PROGRAMS)
#: The capture modes the runner supports per purpose. A rear comparison reads each woofer solo as well as their sum, so it is the one non-speaker purpose a :data:`REGIME_BRANCHES` take may carry (issue #5330).
_REGIMES_BY_PURPOSE = {name: next((row.regimes for row in _PROGRAM_SECTIONS if row.purpose == name),
                                (REGIME_SUMMED,)) for name in PURPOSES}
# A reference take may play one driver alone on any regime (ADR-0366); see validated_pose_driver.
_REGIMES_BY_PURPOSE[PURPOSE_REFERENCE] = REGIMES
#: The layout a run banks when its poses are its own inline list, not a named layout's.
CUSTOM_LAYOUT = "custom"
GRAPH_LAYERS = tuple(row.candidate_fields[0].name for row in PROGRAM_DOCUMENT_ORDER if row.graph_evidence)
#: Each program's own candidate layer: the names a take may play cleared (ADR-0370).
CANDIDATE_LAYERS = tuple(row.candidate_fields[0].name for row in PROGRAM_DOCUMENT_ORDER)


def program_entries(topology: OutputTopology) -> tuple[dict[str, Any], ...]:
    return tuple(entry for entry in PROGRAM_ENTRIES if entry["id"] in programs_for_topology(topology))


def programs_for_topology(topology: OutputTopology) -> tuple[str, ...]:
    passive = topology_is_subless_passive_mains(topology)
    rear = any(cardioid_cabinet_channels(
        (channel.role, channel.output_variant, channel.physical_output_index)
        for channel in group.channels
        if channel.physical_output_index is not None
    ) is not None for group in topology.speaker_groups)
    return tuple(name for name in RUNNABLE_PROGRAMS
                 if not (name == PURPOSE_SPEAKER and passive or name == PURPOSE_REAR and not rear))


def cleared_layers(purpose: str | None, *, base: bool, regime: str) -> tuple[str, ...]:
    """The applied candidate layers a take of ``purpose`` in ``regime`` plays
    cleared, on the run's base or on a candidate it names (ADR-0370)."""
    row = next((row for row in _PROGRAM_SECTIONS if row.purpose == purpose), None)
    if row is None:
        return ()
    own = base and row.base_clears_own or regime == REGIME_BRANCHES and row.branches_clear_own
    return row.clears + ((row.candidate_fields[0].name,) if own else ())


def near_field_drivers(topology: OutputTopology) -> tuple[str, ...]:
    """The drivers a pose may play alone here: every measured driver of a mono
    speaker, and none of a stereo pair's until #5697 (ADR-0360)."""
    return tuple(sorted(
        measurement_target_id(target["role"], target.get("output_variant", "primary"))
        for target in active_driver_targets(topology)
        if target["speaker_group_id"] == topology.routing.mono_group_id))


#: WHICH two measurement targets a :data:`REGIME_BRANCHES` take excites: the
#: declared driver roles, or the front and rear woofer of a cardioid cabinet
#: (ADR-0316). Resolved to target ids by
#: :func:`~.crossover_v2.measure_spec.branch_target_ids_for`.
BRANCH_PAIR_DRIVERS = "drivers"
BRANCH_PAIR_FRONT_REAR = "front_rear"
BRANCH_PAIRS = (BRANCH_PAIR_DRIVERS, BRANCH_PAIR_FRONT_REAR)


def _validated_purpose(purpose: str | None) -> str:
    """The purpose a stop or take names; one that names none refuses (#2902)."""
    if purpose is None or purpose not in PURPOSES:
        raise ValueError(f"a measurement purpose must be one of {PURPOSES}, got {purpose!r}")
    return purpose


def validated_capture_purpose(purpose: str | None, regime: str) -> str:
    """The purpose a stop names, in a capture mode the runner supports for it."""
    resolved = _validated_purpose(purpose)
    if regime not in REGIMES:
        raise ValueError(f"a measurement regime must be one of {REGIMES}, got {regime!r}")
    supported = _REGIMES_BY_PURPOSE[resolved]
    if regime not in supported:
        raise ValueError(f"{resolved} measurements require one of {supported}, got {regime!r}")
    return resolved


def validated_pose_driver(driver: str, *, regime: str, purpose: str | None) -> str:
    """The one driver a pose plays alone, a measurement target id, at any kind
    and distance (ADR-0366). Only a reference pose names one, until a tuning
    reader admits a one-driver take, and one on any regime but summed must."""
    if not isinstance(driver, str):
        raise ValueError(f"a pose driver is a measurement target id, got {driver!r}")
    if driver and purpose != PURPOSE_REFERENCE:
        raise ValueError(f"only a {PURPOSE_REFERENCE} pose names its driver")
    if not driver and purpose == PURPOSE_REFERENCE and regime != REGIME_SUMMED:
        raise ValueError(f"a {PURPOSE_REFERENCE} {regime} pose names the one driver it plays")
    return driver


def validated_purposes(purposes: Sequence[str], regime: str, drivers: Collection[str]) -> tuple[str, ...]:
    """What a preset's or a stop's takes serve, each named once. Every purpose,
    not only the first, must admit the regime and each driver a pose plays
    alone, so their order never decides what loads (ADR-0383)."""
    purposes = tuple(purposes)
    if not purposes or len(set(purposes)) < len(purposes):
        raise ValueError("a measurement names its purposes, each once")
    for purpose in purposes:
        validated_capture_purpose(purpose, regime)
        for driver in drivers:
            validated_pose_driver(driver, regime=regime, purpose=purpose)
    return purposes


def validated_branch_pair(branch_pair: str, regime: str) -> str:
    """The branch pair a take names, judged against the regime that plays it."""
    if branch_pair not in BRANCH_PAIRS:
        raise ValueError(f"a branch pair must be one of {BRANCH_PAIRS}, got {branch_pair!r}")
    if branch_pair != BRANCH_PAIR_DRIVERS and regime != REGIME_BRANCHES:
        raise ValueError(f"branch_pair {branch_pair!r} requires the {REGIME_BRANCHES} regime")
    return branch_pair


LAYOUT_NOT_OFFERED = "measurement_layout_not_offered"
POSES_NAME_A_LAYOUT = "measurement_poses_name_a_layout"
DRIVER_NOT_OFFERED = "measurement_driver_not_offered"


def run_purpose(banked: str | None) -> str:
    """The purpose behind a run manifest's preset id (``speaker/mark``) or bare program
    name (``speaker``); any other id refuses as an unknown preset."""
    banked = str(banked or "")
    if not banked or banked in PURPOSES:
        return banked
    return preset(banked).purpose


def run_purposes(banked: str) -> tuple[str, ...]:
    """The purposes of a run manifest's preset, its program's first."""
    try:
        return preset(banked).purposes
    except UnknownPresetError:
        return (run_purpose(banked),)


def gate_exemption(kind: str | None, *, driver: str = "", distance_m: float | None = None) -> str | None:
    """Why a take at this pose is read ungated: a seat pose is the room's own
    measurement; a pose within the near-field distance of one driver reads the
    room about 40 dB down. A take's purpose never exempts it (ADR-0400)."""
    from jasper.audio_measurement.gating import NEAR_FIELD_EXEMPT  # lazy: keeps jasper.web numpy-free (tests/test_correction_substream_ssot.py)

    if kind == POSE_KIND_SEAT:
        return POSE_KIND_SEAT
    return NEAR_FIELD_EXEMPT if at_driver_near_field(driver, distance_m) else None


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
    driver: str = "",
) -> tuple[object, ...]:
    """What distinguishes one microphone position from another; a pose at a
    driver is also that driver's, so the front and rear woofer at one distance
    are two placements."""
    place = (kind, azimuth_deg, elevation_deg, distance_m, seat_offset_m)
    return (*place, driver) if driver else place


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
    #: The one driver this pose plays and sits at, a measurement target id
    #: (``woofer``, ``woofer:rear``); empty when the pose plays the program's own
    #: scope (:func:`validated_pose_driver`).
    driver: str = ""

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
            self.distance_m, self.seat_offset_m, self.driver,
        )


@dataclass(frozen=True)
class Preset:
    """One registry row: its id (``speaker/mark``), what plays, and its poses at a layout it
    offers (ADR-0366 §6)."""
    preset: str
    poses: tuple[ProgramPose, ...]
    #: What its takes serve, its program's first (ADR-0336, ADR-0383).
    purposes: tuple[str, ...]
    regime: str = REGIME_PER_DRIVER
    mover: str | None = None
    layout: str = ""
    levels: str | None = None
    stimulus: Mapping[str, Any] | None = None
    branch_pair: str = BRANCH_PAIR_DRIVERS
    #: The named layouts this preset offers; ``layout`` is the one these poses are,
    #: or :data:`CUSTOM_LAYOUT` for an inline list (ADR-0366 §6).
    layouts: tuple[str, ...] = ()
    #: What it plays and when to run it, one sentence each, for the agent's catalog.
    description: str = ""
    use_when: str = ""
    #: Whether a run takes the ADR-0319 timing take, MEASURE's in-session prior.
    timing_take: bool = False

    def __post_init__(self) -> None:
        if not self.poses:
            raise ValueError("a measurement preset must contain at least one pose")
        object.__setattr__(self, "purposes", validated_purposes(
            self.purposes, self.regime, [pose.driver for pose in self.poses]))
        validated_branch_pair(self.branch_pair, self.regime)
        if not isinstance(self.timing_take, bool):
            raise ValueError("timing_take must be a boolean")

    @property
    def purpose(self) -> str:
        return self.purposes[0]

    @property
    def mic_move_count(self) -> int:
        """Distinct places — repeats stay at one place and move nothing."""

        return len({p.place for p in self.poses})

    @property
    def capture_count(self) -> int:
        return sum(p.repeats for p in self.poses)


@dataclass(frozen=True)
class Layout:
    """A named pose set that presets offer, with the mover it pins (ADR-0366 §6)."""
    poses: tuple[ProgramPose, ...]
    mover: str | None
    description: str
    use_when: str


class LayoutNotOfferedError(ValueError):
    """A named layout the run's preset does not offer; ``detail`` names the ones it does."""

    reason = LAYOUT_NOT_OFFERED

    def __init__(self, preset: str, layout: str, offered: tuple[str, ...]) -> None:
        self.detail = {"preset": preset, "layout": layout, "offered": list(offered)}
        super().__init__(f"{preset} offers {', '.join(offered)}, not {layout}")


class PosesNameALayoutError(ValueError):
    """``poses`` names a layout; a named layout is chosen with ``layout``."""

    reason = POSES_NAME_A_LAYOUT

    def __init__(self, layout: str) -> None:
        self.detail = {"poses": layout, "use": "--layout"}
        super().__init__(f"{layout} is a layout: pass it as --layout")


class DriverNotOfferedError(ValueError):
    """A run narrowed to a driver its preset cannot play alone here; ``detail``
    names the declared outputs the preset does play alone here."""

    reason = DRIVER_NOT_OFFERED

    def __init__(self, preset: str, driver: str, offered: Sequence[str]) -> None:
        self.detail = {"preset": preset, "driver": driver, "offered": list(offered)}
        super().__init__(f"{preset} cannot play {driver} alone here")


class UnknownPresetError(ValueError):
    """No such preset id or program name. ``choices`` carries the preset ids."""

    def __init__(self, preset: str, choices: tuple[str, ...]) -> None:
        self.preset = preset
        self.choices = choices
        super().__init__(f"no measurement preset {preset}; choose one of: {', '.join(choices) or '(none)'}")


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


def _load_presets(path: str | Path | None = None) -> tuple[Mapping[str, Preset], Mapping[str, Layout]]:
    raw = json.loads(_config_text(path))
    if not isinstance(raw, dict):
        raise ValueError("measurement plan must be an object")
    unknown = set(raw) - {"layouts", "presets", "stimuli"}
    if unknown:
        raise ValueError(f"measurement plan has unknown fields: {sorted(unknown)}")

    stimuli = raw.get("stimuli", {})
    if not isinstance(stimuli, dict):
        raise ValueError("measurement plan stimuli must be an object")
    for name, spec in stimuli.items():
        _text(name, "stimulus name")
        if not isinstance(spec, dict) or set(spec) != {"ceiling_hz"}:
            raise ValueError(f"stimulus {name!r} must contain only ceiling_hz")
        ceiling = spec["ceiling_hz"]
        if isinstance(ceiling, bool) or not isinstance(ceiling, (int, float)) or not 0 < ceiling < math.inf:
            raise ValueError(f"stimulus {name!r} ceiling_hz must be positive and finite")

    layouts_raw = raw.get("layouts")
    if not isinstance(layouts_raw, dict) or not layouts_raw:
        raise ValueError("measurement plan layouts must be a nonempty object")
    layouts: dict[str, Layout] = {}
    for name, values in layouts_raw.items():
        name = _text(name, "layout name")
        if not isinstance(values, dict) or set(values) - {"poses", "mover", "description", "use_when"}:
            raise ValueError(f"layout {name!r} must be an object of poses, mover, description and use_when")
        poses = values.get("poses")
        if not isinstance(poses, list) or not poses:
            raise ValueError(f"layout {name!r} must contain at least one pose")
        layouts[name] = Layout(
            tuple(_pose(value, name, index) for index, value in enumerate(poses)),
            _text(values["mover"], f"layout {name!r} mover") if "mover" in values else None,
            *(_text(values.get(key), f"layout {name!r} {key}") for key in ("description", "use_when")))

    rows = raw.get("presets")
    if not isinstance(rows, list) or not rows:
        raise ValueError("measurement plan presets must be a nonempty list")
    presets: dict[str, Preset] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"preset {index} must be an object")
        unknown = set(row) - {"preset", "layout", "layouts", "purposes", "regime", "levels", "stimulus",
                              "branch_pair", "description", "use_when", "timing_take"}
        if unknown:
            raise ValueError(f"preset {index} has unknown fields: {sorted(unknown)}")
        try:
            preset_id = _text(row["preset"], f"preset {index} id")
            layout = _text(row["layout"], f"preset {index} layout")
            purposes = row["purposes"]
        except KeyError as exc:
            raise ValueError(f"preset {index} is missing {exc.args[0]}") from None
        offered = row.get("layouts", [layout])
        if not isinstance(offered, list) or layout not in offered:
            raise ValueError(f"preset {preset_id} must offer its default layout {layout!r}")
        for name in offered:
            if name not in layouts:
                raise ValueError(f"preset {preset_id} names unknown layout {name!r}")
        stimulus = row.get("stimulus")
        if stimulus is not None:
            _text(stimulus, f"preset {preset_id} stimulus")
            if stimulus not in stimuli:
                raise ValueError(f"preset {preset_id} names unknown stimulus {stimulus!r}")
        if preset_id in presets:
            raise ValueError(f"measurement plan repeats preset {preset_id}")
        levels = row.get("levels")
        if levels not in (None, "auto"):
            raise ValueError(f"preset {preset_id} levels must be 'auto', got {levels!r}")
        if not isinstance(purposes, list):
            raise ValueError(f"preset {preset_id} purposes must be a list")
        presets[preset_id] = Preset(
            preset_id,
            layouts[layout].poses,
            purposes=tuple(_text(value, f"preset {preset_id} purpose") for value in purposes),
            regime=row.get("regime", REGIME_PER_DRIVER),
            mover=layouts[layout].mover,
            layout=layout, layouts=tuple(offered), levels=levels,
            stimulus=stimuli[stimulus] if stimulus is not None else None,
            branch_pair=row.get("branch_pair", BRANCH_PAIR_DRIVERS),
            description=_text(row.get("description"), f"preset {preset_id} description"),
            use_when=_text(row.get("use_when"), f"preset {preset_id} use_when"),
            timing_take=row.get("timing_take", False),
        )
    return MappingProxyType(presets), MappingProxyType(layouts)


def load_presets(path: str | Path | None = None) -> Mapping[str, Preset]:
    """Load and validate the bundled plan, or a plan at ``path``."""
    return _load_presets(path)[0]


_PRESETS, _LAYOUTS = _load_presets()


def prescription_sections(purpose: str | None = None) -> tuple[str, ...]:
    """The prescription sections one program owns; every program's when ``purpose`` is None."""
    return tuple(section.name for row in _PROGRAM_SECTIONS
                 if purpose is None or row.purpose == purpose for section in row.sections if section.reset)

def available_presets() -> tuple[str, ...]:
    """The preset ids a menu may offer, sorted."""
    return tuple(sorted(_PRESETS))


def preset(name: str) -> Preset:
    """The preset with this id (``rear/pair``); a bare program name (``rear``) is the
    first of its presets in registry order."""
    found = _PRESETS.get(name) or next(
        (row for preset_id, row in _PRESETS.items() if preset_id.partition("/")[0] == name), None)
    if found is None:
        raise UnknownPresetError(name, available_presets())
    return found


def named_layout(name: str) -> Layout:
    """The layout a preset offers by this name (``seat_express``)."""
    return _LAYOUTS[name]


def run_preset(name: str, layout: str | None = None, poses: str | Sequence[Any] | None = None) -> Preset:
    """Resolve a run's preset (a bare program name is its first preset) at a named
    layout it offers, or at an inline list of pose objects or whole-degree bearings,
    given as a list or as text: a JSON list or comma-separated bearings (ADR-0298,
    ADR-0366 §6)."""
    selected = preset(name)
    if layout is not None:
        if layout not in selected.layouts:
            raise LayoutNotOfferedError(selected.preset, layout, selected.layouts)
        named = _LAYOUTS[layout]
        selected = replace(selected, layout=layout, poses=named.poses, mover=named.mover)
    if poses is None:
        return selected
    if isinstance(poses, str):
        if poses in _LAYOUTS:
            raise PosesNameALayoutError(poses)
        poses = json.loads(poses) if poses.lstrip().startswith("[") else [int(value) for value in poses.split(",")]
    return replace(selected, layout=CUSTOM_LAYOUT, poses=tuple(
        _pose({"azimuth_deg": value, "elevation_deg": 0} if isinstance(value, int) else value, CUSTOM_LAYOUT, index)
        for index, value in enumerate(poses)))


def plan_poses(preset: Preset, targets: Sequence[str] = (), driver: str = "") -> tuple[ProgramPose, ...]:
    """The poses a run walks on this speaker. A named layout's pose that names a bare
    driver role (``woofer``) plays each declared output of that role (``targets``), one
    output's poses after the other's, so a cardioid's rear woofer follows its front one
    (ADR-0366 §6); a role with no declared output keeps its name for preflight to refuse.
    A pose that names one output (``woofer:rear``), and every inline pose, plays what it
    names. ``driver`` narrows the run to that one output."""
    runs = [(named, tuple(run)) for named, run in groupby(preset.poses, key=lambda pose: pose.driver)]
    poses = tuple(replace(pose, driver=output) for named, run in runs
                  for output in (_outputs(named, targets) if preset.layout != CUSTOM_LAYOUT else (named,))
                  for pose in run)
    if not driver:
        return poses
    narrowed = tuple(pose for pose in poses if pose.driver == driver)
    if driver not in targets or not narrowed:
        raise DriverNotOfferedError(preset.preset, driver,
                                    tuple(dict.fromkeys(pose.driver for pose in poses if pose.driver in targets)))
    return narrowed


def _outputs(named: str, targets: Sequence[str]) -> tuple[str, ...]:
    """Each declared output of the role a layout pose names; one output (``woofer:rear``)
    is no role, so it, like a role with no declared output, plays as named."""
    return tuple(target for target in targets if measurement_target_parts(target)[0] == named) or (named,)


def offered_here(plan: Preset, *, programs: Collection[str], targets: Collection[str]) -> bool:
    """Whether a speaker offering ``programs`` (:func:`programs_for_topology`), whose
    ``targets`` each play alone (:func:`near_field_drivers`), runs this preset at its
    layout. The measure page offers only these, and the preset catalog prices only these."""
    return not ((plan.purpose in RUNNABLE_PROGRAMS and plan.purpose not in programs)
                or (plan.branch_pair == BRANCH_PAIR_FRONT_REAR and PURPOSE_REAR not in programs)
                or not {pose.driver for pose in plan.poses if pose.driver} <= set(targets))


def trial_preset(
    sections: Collection[str], mover: str | None = None, layout: str | None = None,
) -> Preset | None:
    """The first program the document states of rear, bass, room, speaker (reverse document
    order): its first trial preset that offers ``layout``, else its first trial layout
    ``mover`` can walk; ``None`` when it states none."""
    row = next((row for row in reversed(PROGRAM_DOCUMENT_ORDER)
                if any(section.name in sections for section in row.sections)), None)
    if row is None:
        return None
    trials = [run_preset(trial, trial_layout) for trial, trial_layout in row.trial]
    return next((trial for trial in trials if layout in trial.layouts), None) or next(
        (trial for trial in trials if mover is None or trial.mover in (None, mover)), trials[0])


BASE_CANDIDATE = "base"


def candidate_identity(value: str, *, for_spec: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("candidate_id must be text")
    if value not in ("", BASE_CANDIDATE):
        return value
    return "" if for_spec else BASE_CANDIDATE
