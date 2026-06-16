"""Validated data model for active speaker baseline commissioning.

Active speaker commissioning is the Layer-A speaker baseline: channel map,
driver roles, crossover regions, protection assumptions, and acceptance gates.
This module deliberately has no CamillaDSP side effects. Later generator and
wizard code should consume these dataclasses instead of accepting freeform YAML.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1
ACTIVE_PRESET_KIND = "jts_active_speaker_preset"
ACTIVE_BASELINE_KIND = "jts_speaker_baseline_profile"

DRIVER_ROLES_BY_WAY: dict[int, tuple[str, ...]] = {
    2: ("woofer", "tweeter"),
    3: ("woofer", "mid", "tweeter"),
}
ADJACENT_PAIRS_BY_WAY: dict[int, tuple[tuple[str, str], ...]] = {
    2: (("woofer", "tweeter"),),
    3: (("woofer", "mid"), ("mid", "tweeter")),
}
SUPPORTED_LAYOUTS = {"mono", "stereo"}
SIDES_BY_LAYOUT: dict[str, tuple[str, ...]] = {
    "mono": ("mono",),
    "stereo": ("left", "right"),
}
SUPPORTED_CROSSOVER_TYPES = {"LinkwitzRiley"}
SUPPORTED_LR_ORDERS = {2, 4, 8}
SUPPORTED_POLARITY = {"non-inverted", "inverted"}
BASELINE_STATUSES = {
    "draft",
    "channel_safety_verified",
    "measurement_in_progress",
    "commissioned",
    "rejected",
}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")


class ActiveSpeakerConfigError(ValueError):
    """Raised when an active-speaker preset or baseline is unsafe/invalid."""


def required_driver_roles(way_count: int) -> tuple[str, ...]:
    try:
        return DRIVER_ROLES_BY_WAY[int(way_count)]
    except (KeyError, TypeError, ValueError) as e:
        raise ActiveSpeakerConfigError("way_count must be 2 or 3") from e


def _required_sides(layout: str) -> tuple[str, ...]:
    if layout not in SUPPORTED_LAYOUTS:
        raise ActiveSpeakerConfigError("layout must be 'mono' or 'stereo'")
    return SIDES_BY_LAYOUT[layout]


def _require_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActiveSpeakerConfigError(f"{field_name} is required")
    out = value.strip()
    if not _ID_RE.match(out):
        raise ActiveSpeakerConfigError(
            f"{field_name} must be <=80 chars and contain only safe id chars"
        )
    return out


def _require_text(value: Any, field_name: str, *, max_chars: int = 120) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActiveSpeakerConfigError(f"{field_name} is required")
    out = " ".join(value.split())
    if len(out) > max_chars:
        raise ActiveSpeakerConfigError(f"{field_name} must be <= {max_chars} chars")
    return out


def _optional_text(value: Any, *, max_chars: int = 160) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ActiveSpeakerConfigError("optional text fields must be strings")
    out = " ".join(value.split())
    if len(out) > max_chars:
        raise ActiveSpeakerConfigError(f"text field must be <= {max_chars} chars")
    return out


def _finite_float(value: Any, field_name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as e:
        raise ActiveSpeakerConfigError(f"{field_name} must be numeric") from e
    if not math.isfinite(out):
        raise ActiveSpeakerConfigError(f"{field_name} must be finite")
    return out


def _integer(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise ActiveSpeakerConfigError(f"{field_name} must be an integer") from e


def _optional_float(value: Any, field_name: str) -> float | None:
    if value is None or value == "":
        return None
    return _finite_float(value, field_name)


def _positive_float(value: Any, field_name: str) -> float:
    out = _finite_float(value, field_name)
    if out <= 0:
        raise ActiveSpeakerConfigError(f"{field_name} must be > 0")
    return out


def _bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ActiveSpeakerConfigError(f"{field_name} must be boolean")


def _sequence(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ActiveSpeakerConfigError(f"{field_name} must be a list")
    return value


@dataclass(frozen=True)
class DriverSpec:
    """One physical driver role in a known active-speaker preset."""

    role: str
    manufacturer: str
    model: str
    fs_hz: float | None = None
    sensitivity_db: float | None = None
    rated_power_w: float | None = None
    expected_nearfield_response: str | None = None

    @classmethod
    def from_mapping(cls, role: str, raw: Any) -> "DriverSpec":
        if not isinstance(raw, dict):
            raise ActiveSpeakerConfigError(f"driver {role} must be an object")
        declared = raw.get("role", role)
        if declared != role:
            raise ActiveSpeakerConfigError(f"driver role mismatch for {role}")
        return cls(
            role=role,
            manufacturer=_require_text(raw.get("manufacturer"), f"{role}.manufacturer"),
            model=_require_text(raw.get("model"), f"{role}.model"),
            fs_hz=_optional_float(raw.get("fs_hz"), f"{role}.fs_hz"),
            sensitivity_db=_optional_float(
                raw.get("sensitivity_db"), f"{role}.sensitivity_db"
            ),
            rated_power_w=_optional_float(raw.get("rated_power_w"), f"{role}.rated_power_w"),
            expected_nearfield_response=_optional_text(
                raw.get("expected_nearfield_response")
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "role": self.role,
            "manufacturer": self.manufacturer,
            "model": self.model,
        }
        for key in (
            "fs_hz",
            "sensitivity_db",
            "rated_power_w",
            "expected_nearfield_response",
        ):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


@dataclass(frozen=True)
class OutputChannel:
    """One physical output lane before drivers are connected."""

    index: int
    side: str
    driver_role: str
    label: str
    startup_muted: bool = True

    @classmethod
    def from_mapping(cls, raw: Any) -> "OutputChannel":
        if not isinstance(raw, dict):
            raise ActiveSpeakerConfigError("output channel must be an object")
        index = _integer(raw.get("index"), "output.index")
        if index < 0:
            raise ActiveSpeakerConfigError("output index must be >= 0")
        side = _require_id(raw.get("side"), "output.side")
        driver_role = _require_id(raw.get("driver_role"), "output.driver_role")
        return cls(
            index=index,
            side=side,
            driver_role=driver_role,
            label=_require_text(raw.get("label"), "output.label", max_chars=80),
            startup_muted=_bool(raw.get("startup_muted", True), "output.startup_muted"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "side": self.side,
            "driver_role": self.driver_role,
            "label": self.label,
            "startup_muted": self.startup_muted,
        }


@dataclass(frozen=True)
class ActiveChannelMap:
    """Expected active output topology for a preset or baseline."""

    layout: str
    outputs: tuple[OutputChannel, ...]

    @classmethod
    def from_mapping(cls, raw: Any) -> "ActiveChannelMap":
        if not isinstance(raw, dict):
            raise ActiveSpeakerConfigError("channel_map must be an object")
        layout = str(raw.get("layout") or "").strip().lower()
        _required_sides(layout)
        outputs = tuple(
            OutputChannel.from_mapping(item)
            for item in _sequence(raw.get("outputs"), "channel_map.outputs")
        )
        return cls(layout=layout, outputs=outputs)

    def validate_for_way(self, way_count: int) -> None:
        roles = required_driver_roles(way_count)
        sides = _required_sides(self.layout)
        seen_indexes: set[int] = set()
        seen_slots: set[tuple[str, str]] = set()
        for output in self.outputs:
            if output.index in seen_indexes:
                raise ActiveSpeakerConfigError(f"duplicate output index {output.index}")
            seen_indexes.add(output.index)
            if not output.startup_muted:
                raise ActiveSpeakerConfigError(
                    f"output {output.index} must start muted for commissioning safety"
                )
            if output.side not in sides:
                raise ActiveSpeakerConfigError(
                    f"output side {output.side!r} invalid for {self.layout}"
                )
            if output.driver_role not in roles:
                raise ActiveSpeakerConfigError(
                    f"output driver role {output.driver_role!r} invalid for {way_count}-way"
                )
            slot = (output.side, output.driver_role)
            if slot in seen_slots:
                raise ActiveSpeakerConfigError(
                    f"duplicate output for {output.side}/{output.driver_role}"
                )
            seen_slots.add(slot)
        required = {(side, role) for side in sides for role in roles}
        missing = sorted(required - seen_slots)
        extra = sorted(seen_slots - required)
        if missing:
            raise ActiveSpeakerConfigError(f"missing output channels: {missing}")
        if extra:
            raise ActiveSpeakerConfigError(f"unexpected output channels: {extra}")
        expected_indexes = set(range(len(self.outputs)))
        if seen_indexes != expected_indexes:
            raise ActiveSpeakerConfigError(
                "output indexes must be contiguous CamillaDSP channel indexes from 0"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "layout": self.layout,
            "outputs": [output.to_dict() for output in self.outputs],
        }


@dataclass(frozen=True)
class CrossoverRegion:
    """One adjacent-driver crossover region."""

    id: str
    lower_driver: str
    upper_driver: str
    fc_hz: float
    target_type: str = "LinkwitzRiley"
    order: int = 4
    lower_polarity: str = "non-inverted"
    upper_polarity: str = "non-inverted"
    delay_target_driver: str | None = None
    delay_range_ms: tuple[float, float] = (0.0, 1.0)
    null_depth_threshold_db: float = 25.0

    @classmethod
    def from_mapping(cls, raw: Any) -> "CrossoverRegion":
        if not isinstance(raw, dict):
            raise ActiveSpeakerConfigError("crossover region must be an object")
        order = _integer(raw.get("order", 4), "crossover.order")
        delay_range = raw.get("delay_range_ms", [0.0, 1.0])
        if (
            not isinstance(delay_range, (list, tuple))
            or len(delay_range) != 2
        ):
            raise ActiveSpeakerConfigError("delay_range_ms must have two numbers")
        return cls(
            id=_require_id(raw.get("id"), "crossover.id"),
            lower_driver=_require_id(raw.get("lower_driver"), "crossover.lower_driver"),
            upper_driver=_require_id(raw.get("upper_driver"), "crossover.upper_driver"),
            fc_hz=_positive_float(raw.get("fc_hz"), "crossover.fc_hz"),
            target_type=str(raw.get("target_type") or "LinkwitzRiley"),
            order=order,
            lower_polarity=str(raw.get("lower_polarity") or "non-inverted"),
            upper_polarity=str(raw.get("upper_polarity") or "non-inverted"),
            delay_target_driver=(
                _require_id(raw.get("delay_target_driver"), "crossover.delay_target_driver")
                if raw.get("delay_target_driver") not in {None, ""}
                else None
            ),
            delay_range_ms=(
                _finite_float(delay_range[0], "delay_range_ms[0]"),
                _finite_float(delay_range[1], "delay_range_ms[1]"),
            ),
            null_depth_threshold_db=_positive_float(
                raw.get("null_depth_threshold_db", 25.0),
                "crossover.null_depth_threshold_db",
            ),
        )

    def validate_for_way(self, way_count: int) -> None:
        roles = required_driver_roles(way_count)
        if self.lower_driver not in roles or self.upper_driver not in roles:
            raise ActiveSpeakerConfigError(
                f"crossover {self.id} references unknown driver role"
            )
        if (self.lower_driver, self.upper_driver) not in ADJACENT_PAIRS_BY_WAY[way_count]:
            raise ActiveSpeakerConfigError(
                f"crossover {self.id} must join adjacent {way_count}-way drivers"
            )
        if self.target_type not in SUPPORTED_CROSSOVER_TYPES:
            raise ActiveSpeakerConfigError(f"unsupported crossover type {self.target_type}")
        if self.order not in SUPPORTED_LR_ORDERS:
            raise ActiveSpeakerConfigError("Linkwitz-Riley order must be 2, 4, or 8")
        if self.lower_polarity not in SUPPORTED_POLARITY:
            raise ActiveSpeakerConfigError("lower_polarity is invalid")
        if self.upper_polarity not in SUPPORTED_POLARITY:
            raise ActiveSpeakerConfigError("upper_polarity is invalid")
        if self.delay_target_driver and self.delay_target_driver not in {
            self.lower_driver,
            self.upper_driver,
        }:
            raise ActiveSpeakerConfigError(
                "delay_target_driver must be one of the crossover drivers"
            )
        lo, hi = self.delay_range_ms
        if lo < 0 or hi < 0 or lo > hi or hi > 20:
            raise ActiveSpeakerConfigError(
                "delay_range_ms must be ordered, non-negative, and <= 20 ms"
            )
        if self.null_depth_threshold_db < 15:
            raise ActiveSpeakerConfigError("null depth threshold is too weak")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "lower_driver": self.lower_driver,
            "upper_driver": self.upper_driver,
            "fc_hz": self.fc_hz,
            "target_type": self.target_type,
            "order": self.order,
            "lower_polarity": self.lower_polarity,
            "upper_polarity": self.upper_polarity,
            "delay_target_driver": self.delay_target_driver,
            "delay_range_ms": list(self.delay_range_ms),
            "null_depth_threshold_db": self.null_depth_threshold_db,
        }


@dataclass(frozen=True)
class SafetyEnvelope:
    """Commissioning bounds that keep hardware bring-up conservative."""

    initial_sweep_level_db_spl: float = 65.0
    max_commissioning_level_db_spl: float = 85.0
    escalation_step_db: float = 5.0
    require_physical_tweeter_protection: bool = True
    require_channel_identity_before_drivers: bool = True
    emergency_stop_required: bool = True

    @classmethod
    def from_mapping(cls, raw: Any) -> "SafetyEnvelope":
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ActiveSpeakerConfigError("safety must be an object")
        return cls(
            initial_sweep_level_db_spl=_finite_float(
                raw.get("initial_sweep_level_db_spl", 65.0),
                "initial_sweep_level_db_spl",
            ),
            max_commissioning_level_db_spl=_finite_float(
                raw.get("max_commissioning_level_db_spl", 85.0),
                "max_commissioning_level_db_spl",
            ),
            escalation_step_db=_positive_float(
                raw.get("escalation_step_db", 5.0), "escalation_step_db"
            ),
            require_physical_tweeter_protection=_bool(
                raw.get("require_physical_tweeter_protection", True),
                "require_physical_tweeter_protection",
            ),
            require_channel_identity_before_drivers=_bool(
                raw.get("require_channel_identity_before_drivers", True),
                "require_channel_identity_before_drivers",
            ),
            emergency_stop_required=_bool(
                raw.get("emergency_stop_required", True),
                "emergency_stop_required",
            ),
        )

    def validate(self) -> None:
        if not 45 <= self.initial_sweep_level_db_spl <= 85:
            raise ActiveSpeakerConfigError("initial sweep level must be 45-85 dB SPL")
        if not 45 <= self.max_commissioning_level_db_spl <= 85:
            raise ActiveSpeakerConfigError(
                "max commissioning level must be 45-85 dB SPL"
            )
        if self.initial_sweep_level_db_spl > self.max_commissioning_level_db_spl:
            raise ActiveSpeakerConfigError("initial sweep level must be <= max level")
        if self.escalation_step_db > 10:
            raise ActiveSpeakerConfigError("escalation step must be <= 10 dB")
        if not self.emergency_stop_required:
            raise ActiveSpeakerConfigError("emergency stop must be required")
        if not self.require_channel_identity_before_drivers:
            raise ActiveSpeakerConfigError(
                "channel identity must be proven before drivers are connected"
            )
        if not self.require_physical_tweeter_protection:
            raise ActiveSpeakerConfigError(
                "physical tweeter protection is required for the first active-speaker substrate"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_sweep_level_db_spl": self.initial_sweep_level_db_spl,
            "max_commissioning_level_db_spl": self.max_commissioning_level_db_spl,
            "escalation_step_db": self.escalation_step_db,
            "require_physical_tweeter_protection": (
                self.require_physical_tweeter_protection
            ),
            "require_channel_identity_before_drivers": (
                self.require_channel_identity_before_drivers
            ),
            "emergency_stop_required": self.emergency_stop_required,
        }


@dataclass(frozen=True)
class ActiveSpeakerPreset:
    """A named, designer-authored active-speaker commissioning preset."""

    preset_id: str
    name: str
    way_count: int
    channel_map: ActiveChannelMap
    drivers: dict[str, DriverSpec]
    crossover_regions: tuple[CrossoverRegion, ...]
    safety: SafetyEnvelope = field(default_factory=SafetyEnvelope)
    notes: str | None = None

    @classmethod
    def from_mapping(cls, raw: Any) -> "ActiveSpeakerPreset":
        if not isinstance(raw, dict):
            raise ActiveSpeakerConfigError("preset must be an object")
        if raw.get("artifact_schema_version") != SCHEMA_VERSION:
            raise ActiveSpeakerConfigError("unsupported active-speaker schema version")
        if raw.get("kind") != ACTIVE_PRESET_KIND:
            raise ActiveSpeakerConfigError("unsupported active-speaker preset kind")
        way_count = _integer(raw.get("way_count"), "way_count")
        roles = required_driver_roles(way_count)
        raw_drivers = raw.get("drivers")
        if not isinstance(raw_drivers, dict):
            raise ActiveSpeakerConfigError("drivers must be an object")
        drivers = {
            role: DriverSpec.from_mapping(role, raw_drivers.get(role))
            for role in roles
        }
        preset = cls(
            preset_id=_require_id(raw.get("preset_id") or raw.get("id"), "preset_id"),
            name=_require_text(raw.get("name"), "name"),
            way_count=way_count,
            channel_map=ActiveChannelMap.from_mapping(raw.get("channel_map")),
            drivers=drivers,
            crossover_regions=tuple(
                CrossoverRegion.from_mapping(item)
                for item in _sequence(
                    raw.get("crossover_regions"), "crossover_regions"
                )
            ),
            safety=SafetyEnvelope.from_mapping(raw.get("safety")),
            notes=_optional_text(raw.get("notes"), max_chars=800),
        )
        preset.validate()
        return preset

    def validate(self) -> None:
        roles = required_driver_roles(self.way_count)
        if set(self.drivers) != set(roles):
            raise ActiveSpeakerConfigError(
                f"drivers must include exactly these roles: {roles}"
            )
        self.channel_map.validate_for_way(self.way_count)
        expected_pairs = set(ADJACENT_PAIRS_BY_WAY[self.way_count])
        seen_pairs = {(r.lower_driver, r.upper_driver) for r in self.crossover_regions}
        if seen_pairs != expected_pairs:
            raise ActiveSpeakerConfigError(
                f"crossover regions must be exactly {sorted(expected_pairs)}"
            )
        polarity_by_role: dict[str, str] = {}
        for region in self.crossover_regions:
            region.validate_for_way(self.way_count)
            for role, polarity in (
                (region.lower_driver, region.lower_polarity),
                (region.upper_driver, region.upper_polarity),
            ):
                previous = polarity_by_role.setdefault(role, polarity)
                if previous != polarity:
                    raise ActiveSpeakerConfigError(
                        f"driver {role} has inconsistent polarity across crossover regions"
                    )
        self.safety.validate()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": ACTIVE_PRESET_KIND,
            "preset_id": self.preset_id,
            "name": self.name,
            "way_count": self.way_count,
            "channel_map": self.channel_map.to_dict(),
            "drivers": {
                role: self.drivers[role].to_dict()
                for role in required_driver_roles(self.way_count)
            },
            "crossover_regions": [
                region.to_dict() for region in self.crossover_regions
            ],
            "safety": self.safety.to_dict(),
        }
        if self.notes:
            out["notes"] = self.notes
        return out


def crossover_edges_for_role(
    preset: "ActiveSpeakerPreset", role: str
) -> tuple[float | None, float | None]:
    """Return ``(lower_edge_hz, upper_edge_hz)`` for ``role``'s acoustic band.

    A role being the *upper* driver of a crossover means that crossover's ``fc``
    is the role's lower (high-pass) edge; being the *lower* driver means it is
    the role's upper (low-pass) edge. A woofer (no lower crossover) returns
    ``lower_edge=None``; a tweeter (no upper crossover) returns
    ``upper_edge=None``. This is the single source of a role's crossover edges:
    ``tone_plan`` derives its test-tone band from it and
    ``commissioning_capture`` derives the expected passband for the mic-backed
    driver verdict from it.
    """
    lower_edge: float | None = None
    upper_edge: float | None = None
    for region in preset.crossover_regions:
        if region.upper_driver == role:
            lower_edge = region.fc_hz
        if region.lower_driver == role:
            upper_edge = region.fc_hz
    return lower_edge, upper_edge


@dataclass(frozen=True)
class BaselineVerification:
    """Acceptance evidence for a speaker baseline profile."""

    channel_identity_verified: bool = False
    all_paths_protected: bool = False
    per_driver_measurements_captured: bool = False
    crossover_nulls_captured: bool = False
    gated_sum_captured: bool = False

    @classmethod
    def from_mapping(cls, raw: Any) -> "BaselineVerification":
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ActiveSpeakerConfigError("verification must be an object")
        return cls(
            channel_identity_verified=_bool(
                raw.get("channel_identity_verified", False),
                "channel_identity_verified",
            ),
            all_paths_protected=_bool(
                raw.get("all_paths_protected", False), "all_paths_protected"
            ),
            per_driver_measurements_captured=_bool(
                raw.get("per_driver_measurements_captured", False),
                "per_driver_measurements_captured",
            ),
            crossover_nulls_captured=_bool(
                raw.get("crossover_nulls_captured", False),
                "crossover_nulls_captured",
            ),
            gated_sum_captured=_bool(
                raw.get("gated_sum_captured", False), "gated_sum_captured"
            ),
        )

    def commissioned_ready(self) -> bool:
        return all((
            self.channel_identity_verified,
            self.all_paths_protected,
            self.per_driver_measurements_captured,
            self.crossover_nulls_captured,
            self.gated_sum_captured,
        ))

    def to_dict(self) -> dict[str, bool]:
        return {
            "channel_identity_verified": self.channel_identity_verified,
            "all_paths_protected": self.all_paths_protected,
            "per_driver_measurements_captured": self.per_driver_measurements_captured,
            "crossover_nulls_captured": self.crossover_nulls_captured,
            "gated_sum_captured": self.gated_sum_captured,
        }


@dataclass(frozen=True)
class SpeakerBaselineProfile:
    """Versioned speaker baseline state, distinct from room/preference EQ."""

    baseline_id: str
    preset_id: str
    name: str
    way_count: int
    channel_map: ActiveChannelMap
    status: str = "draft"
    verification: BaselineVerification = field(default_factory=BaselineVerification)

    @classmethod
    def from_preset(
        cls,
        preset: ActiveSpeakerPreset,
        *,
        baseline_id: str,
        status: str = "draft",
        verification: BaselineVerification | None = None,
    ) -> "SpeakerBaselineProfile":
        profile = cls(
            baseline_id=_require_id(baseline_id, "baseline_id"),
            preset_id=preset.preset_id,
            name=preset.name,
            way_count=preset.way_count,
            channel_map=preset.channel_map,
            status=status,
            verification=verification or BaselineVerification(),
        )
        profile.validate()
        return profile

    @classmethod
    def from_mapping(cls, raw: Any) -> "SpeakerBaselineProfile":
        if not isinstance(raw, dict):
            raise ActiveSpeakerConfigError("baseline must be an object")
        if raw.get("artifact_schema_version") != SCHEMA_VERSION:
            raise ActiveSpeakerConfigError("unsupported baseline schema version")
        if raw.get("kind") != ACTIVE_BASELINE_KIND:
            raise ActiveSpeakerConfigError("unsupported baseline kind")
        profile = cls(
            baseline_id=_require_id(raw.get("baseline_id"), "baseline_id"),
            preset_id=_require_id(raw.get("preset_id"), "preset_id"),
            name=_require_text(raw.get("name"), "name"),
            way_count=_integer(raw.get("way_count"), "way_count"),
            channel_map=ActiveChannelMap.from_mapping(raw.get("channel_map")),
            status=str(raw.get("status") or "draft"),
            verification=BaselineVerification.from_mapping(raw.get("verification")),
        )
        profile.validate()
        return profile

    def validate(self) -> None:
        required_driver_roles(self.way_count)
        self.channel_map.validate_for_way(self.way_count)
        if self.status not in BASELINE_STATUSES:
            raise ActiveSpeakerConfigError("invalid baseline status")
        if self.status == "commissioned" and not self.verification.commissioned_ready():
            raise ActiveSpeakerConfigError(
                "commissioned baseline requires channel, path, driver, null, and sum evidence"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": ACTIVE_BASELINE_KIND,
            "baseline_id": self.baseline_id,
            "preset_id": self.preset_id,
            "name": self.name,
            "way_count": self.way_count,
            "channel_map": self.channel_map.to_dict(),
            "status": self.status,
            "verification": self.verification.to_dict(),
        }
