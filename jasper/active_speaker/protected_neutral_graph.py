# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Canonical protected-neutral crossover-measurement graph specification.

The confirmed component-safety profile is the sole owner of measurement
protection.  This module normalizes that declaration once; the CamillaDSP
emitter, complex ``P(f)`` evaluation, RAW identity, and validation all consume
the same immutable specification.  Configured crossover transfer ``C`` stays a
separate object because it replaces ``P`` offline and is never emitted here.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Mapping

from jasper.audio_measurement.evidence_identity import (
    ExactDspStateIdentity,
    json_fingerprint,
)
from jasper.audio_measurement.correction_lane import CORRECTION_SUBSTREAM
from jasper.audio_measurement.excitation import (
    AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
)
from jasper.audio_measurement.transfer_composition import LinearTransferSection

ROLE_ORDER = ("woofer", "tweeter")
PROTECTED_NEUTRAL_GRAPH_KIND = "jts_crossover_protected_neutral_graph_v1"
PROTECTED_NEUTRAL_SPEC_KIND = "jts_crossover_protected_neutral_spec_v1"
PROTECTED_NEUTRAL_GRAPH_EXCLUSIONS = (
    "prior_alignment",
    "prior_driver_linearization",
    "room_correction",
    "bass_extension",
    "preference_eq",
    "configured_crossover",
    "candidate_crossover",
)
_FAMILY = "equivalent_or_steeper"
_POLARITY_VALUES = {"non-inverted": 1, "inverted": -1, "normal": 1}


class ProtectedNeutralGraphError(ValueError):
    """A declared protection or graph identity cannot be represented safely."""


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtectedNeutralGraphError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ProtectedNeutralGraphError(f"{field} must be finite")
    return result


@dataclass(frozen=True)
class DeclaredProtectionFilter:
    """One confirmed filter requirement and its conservative LR realization."""

    kind: str
    cutoff_hz: float
    minimum_slope_db_per_octave: float
    family_or_equivalent: str
    emitted_order: int

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any], *, sample_rate_hz: int
    ) -> "DeclaredProtectionFilter":
        expected = {
            "kind",
            "cutoff_hz",
            "minimum_slope_db_per_octave",
            "family_or_equivalent",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise ProtectedNeutralGraphError(
                "required protection filter is not the normalized declaration"
            )
        kind = str(raw["kind"])
        if kind not in {"highpass", "lowpass"}:
            raise ProtectedNeutralGraphError("unsupported required protection kind")
        cutoff = _finite(raw["cutoff_hz"], "protection cutoff_hz")
        slope = _finite(
            raw["minimum_slope_db_per_octave"],
            "protection minimum_slope_db_per_octave",
        )
        family = str(raw["family_or_equivalent"])
        if family != _FAMILY:
            raise ProtectedNeutralGraphError(
                "required protection family cannot be represented conservatively"
            )
        if not 0.0 < cutoff < sample_rate_hz / 2.0 or not 0.0 < slope <= 96.0:
            raise ProtectedNeutralGraphError(
                "required protection cutoff/slope is outside the emitted domain"
            )
        # CamillaDSP's Linkwitz-Riley order contributes 6 dB/octave per order.
        # LR orders are even, so round UP to the smallest representable slope.
        emitted_order = max(2, 2 * math.ceil(slope / 12.0))
        if emitted_order * 6.0 < slope:
            raise ProtectedNeutralGraphError(
                "required protection slope cannot be represented conservatively"
            )
        return cls(kind, cutoff, slope, family, emitted_order)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DeclaredProtectionFilter":
        expected = {
            "kind",
            "cutoff_hz",
            "minimum_slope_db_per_octave",
            "family_or_equivalent",
            "emitted_filter_type",
            "emitted_order",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise ProtectedNeutralGraphError("declared protection schema mismatch")
        if raw["emitted_filter_type"] != "Linkwitz-Riley":
            raise ProtectedNeutralGraphError("declared protection emitter mismatch")
        candidate = cls.from_mapping(
            {key: raw[key] for key in expected if not key.startswith("emitted_")},
            sample_rate_hz=48_000,
        )
        if raw["emitted_order"] != candidate.emitted_order:
            raise ProtectedNeutralGraphError("declared protection order is not canonical")
        return candidate

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "cutoff_hz": self.cutoff_hz,
            "minimum_slope_db_per_octave": self.minimum_slope_db_per_octave,
            "family_or_equivalent": self.family_or_equivalent,
            "emitted_filter_type": "Linkwitz-Riley",
            "emitted_order": self.emitted_order,
        }

    def linear_section(self) -> LinearTransferSection:
        return LinearTransferSection(
            highpass=self.kind == "highpass",
            frequency_hz=self.cutoff_hz,
            order=self.emitted_order,
            reason="component_safety_profile.required_protection_filters",
        )


@dataclass(frozen=True)
class ProtectedRoleSpec:
    role: str
    target_id: str
    target_fingerprint: str
    crossover_search_band_hz: tuple[float, float]
    physical_polarity_sign: int
    physical_polarity_source: str
    filters: tuple[DeclaredProtectionFilter, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProtectedRoleSpec":
        expected = {
            "role",
            "target_id",
            "target_fingerprint",
            "crossover_search_band_hz",
            "physical_polarity_sign",
            "physical_polarity_source",
            "required_protection_filters",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise ProtectedNeutralGraphError("protected role schema mismatch")
        role = str(raw["role"])
        search = raw["crossover_search_band_hz"]
        filters = raw["required_protection_filters"]
        if (
            role not in ROLE_ORDER
            or not isinstance(raw["target_id"], str)
            or not raw["target_id"]
            or not _is_sha256(raw["target_fingerprint"])
            or not isinstance(search, list)
            or len(search) != 2
            or not isinstance(filters, list)
            or raw["physical_polarity_sign"] not in {-1, 1}
            or raw["physical_polarity_source"]
            not in {
                "component_safety_profile.physical_polarity",
                "neutral_plus_one_no_declared_physical_fact",
            }
        ):
            raise ProtectedNeutralGraphError("protected role identity is invalid")
        lo = _finite(search[0], "crossover lower bound")
        hi = _finite(search[1], "crossover upper bound")
        if not 0.0 < lo <= hi:
            raise ProtectedNeutralGraphError("protected role bounds are invalid")
        sign = int(raw["physical_polarity_sign"])
        source = str(raw["physical_polarity_source"])
        if source == "neutral_plus_one_no_declared_physical_fact" and sign != 1:
            raise ProtectedNeutralGraphError("neutral physical polarity must be +1")
        normalized = tuple(DeclaredProtectionFilter.from_dict(item) for item in filters)
        if len({item.kind for item in normalized}) != len(normalized):
            raise ProtectedNeutralGraphError("protected role repeats filter kind")
        if role == "tweeter" and not any(item.kind == "highpass" for item in normalized):
            raise ProtectedNeutralGraphError("protected tweeter high-pass is missing")
        return cls(
            role=role,
            target_id=str(raw["target_id"]),
            target_fingerprint=str(raw["target_fingerprint"]),
            crossover_search_band_hz=(lo, hi),
            physical_polarity_sign=sign,
            physical_polarity_source=source,
            filters=normalized,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "target_id": self.target_id,
            "target_fingerprint": self.target_fingerprint,
            "crossover_search_band_hz": list(self.crossover_search_band_hz),
            "physical_polarity_sign": self.physical_polarity_sign,
            "physical_polarity_source": self.physical_polarity_source,
            "required_protection_filters": [item.to_dict() for item in self.filters],
        }


@dataclass(frozen=True)
class ProtectedNeutralGraphSpec:
    """Every input that can change the emitted protected-neutral graph."""

    profile_fingerprint: str
    preset_id: str
    role_channels: Mapping[str, int]
    output_roles: tuple[tuple[int, str], ...]
    roles: tuple[ProtectedRoleSpec, ...]
    playback_device: str
    capture_device: str
    capture_format: str
    playback_format: str
    sample_rate_hz: int
    chunksize: int
    target_level: int
    limiter_threshold_dbfs: float
    stimulus_peak_ceiling_dbfs: float
    volume_ceiling_db: float
    graph_headroom_gain_db: float = 0.0

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProtectedNeutralGraphSpec":
        expected = {
            "schema_version",
            "kind",
            "profile_fingerprint",
            "preset_id",
            "role_channels",
            "output_roles",
            "roles",
            "devices",
            "limiter",
            "stimulus_peak_ceiling_dbfs",
            "volume_ceiling_db",
            "graph_headroom_gain_db",
            "excluded_filter_layers",
            "transport",
            "lifecycle",
        }
        if (
            not isinstance(raw, Mapping)
            or set(raw) != expected
            or raw["schema_version"] != 1
            or raw["kind"] != PROTECTED_NEUTRAL_SPEC_KIND
            or not _is_sha256(raw["profile_fingerprint"])
            or not isinstance(raw["preset_id"], str)
            or not raw["preset_id"]
            or raw["role_channels"] != {"woofer": 0, "tweeter": 1}
            or raw["excluded_filter_layers"] != list(PROTECTED_NEUTRAL_GRAPH_EXCLUSIONS)
            or raw["transport"]
            != {
                "alsa_device": CORRECTION_SUBSTREAM,
                "physical_program_channels": 2,
                "lane_semantics": {"0": "woofer", "1": "tweeter"},
                "summed_semantics": "sample_exact_identical_pcm_on_both_lanes",
            }
            or raw["lifecycle"]
            != {
                "load": "inline_transient_under_existing_dsp_writer_lock",
                "restore_owner": "capture_entry_anchor",
                "restore": "exact_entry_config_on_success_abort_failure_or_restart",
                "clear": "only_after_confirmed_restore",
                "durable_profile_mutated": False,
                "undo_mutated": False,
            }
        ):
            raise ProtectedNeutralGraphError("protected graph specification mismatch")
        output_roles = raw["output_roles"]
        roles = raw["roles"]
        devices = raw["devices"]
        limiter = raw["limiter"]
        if (
            not isinstance(output_roles, list)
            or not output_roles
            or not all(
                isinstance(item, Mapping)
                and set(item) == {"output_index", "role"}
                and type(item["output_index"]) is int
                and item["output_index"] >= 0
                and item["role"] in ROLE_ORDER
                for item in output_roles
            )
            or not isinstance(roles, list)
            or not isinstance(devices, Mapping)
            or set(devices)
            != {
                "playback_device",
                "capture_device",
                "capture_format",
                "playback_format",
                "sample_rate_hz",
                "chunksize",
                "target_level",
            }
            or not isinstance(limiter, Mapping)
            or limiter.get("type") != "soft_clip"
            or limiter.get("excluded_from_linear_transfer_P") is not True
            or set(limiter) != {"type", "threshold_dbfs", "excluded_from_linear_transfer_P"}
        ):
            raise ProtectedNeutralGraphError("protected graph nested schema mismatch")
        parsed_roles = tuple(ProtectedRoleSpec.from_dict(item) for item in roles)
        if tuple(item.role for item in parsed_roles) != ROLE_ORDER:
            raise ProtectedNeutralGraphError("protected graph role order mismatch")
        parsed_outputs = tuple(
            (int(item["output_index"]), str(item["role"])) for item in output_roles
        )
        if (
            len({item[0] for item in parsed_outputs}) != len(parsed_outputs)
            or set(item[1] for item in parsed_outputs) != set(ROLE_ORDER)
        ):
            raise ProtectedNeutralGraphError("protected graph outputs are invalid")
        sample_rate = devices["sample_rate_hz"]
        chunksize = devices["chunksize"]
        target_level = devices["target_level"]
        if (
            type(sample_rate) is not int
            or sample_rate != 48_000
            or type(chunksize) is not int
            or chunksize <= 0
            or type(target_level) is not int
            or target_level <= 0
            or not all(
                isinstance(devices[key], str) and devices[key]
                for key in (
                    "playback_device",
                    "capture_device",
                    "capture_format",
                    "playback_format",
                )
            )
        ):
            raise ProtectedNeutralGraphError("protected graph device contract is invalid")
        threshold = _finite(limiter["threshold_dbfs"], "limiter threshold")
        stimulus_ceiling = _finite(
            raw["stimulus_peak_ceiling_dbfs"], "stimulus peak ceiling"
        )
        ceiling = _finite(raw["volume_ceiling_db"], "volume ceiling")
        headroom = _finite(raw["graph_headroom_gain_db"], "headroom gain")
        if (
            not -120.0 <= threshold <= 0.0
            or stimulus_ceiling != AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS
            or ceiling > 0.0
            or headroom > 0.0
        ):
            raise ProtectedNeutralGraphError("protected graph gain contract is unsafe")
        return cls(
            profile_fingerprint=str(raw["profile_fingerprint"]),
            preset_id=str(raw["preset_id"]),
            role_channels={"woofer": 0, "tweeter": 1},
            output_roles=parsed_outputs,
            roles=parsed_roles,
            playback_device=str(devices["playback_device"]),
            capture_device=str(devices["capture_device"]),
            capture_format=str(devices["capture_format"]),
            playback_format=str(devices["playback_format"]),
            sample_rate_hz=sample_rate,
            chunksize=chunksize,
            target_level=target_level,
            limiter_threshold_dbfs=threshold,
            stimulus_peak_ceiling_dbfs=stimulus_ceiling,
            volume_ceiling_db=ceiling,
            graph_headroom_gain_db=headroom,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": PROTECTED_NEUTRAL_SPEC_KIND,
            "profile_fingerprint": self.profile_fingerprint,
            "preset_id": self.preset_id,
            "role_channels": {
                role: int(self.role_channels[role]) for role in ROLE_ORDER
            },
            "output_roles": [
                {"output_index": index, "role": role}
                for index, role in self.output_roles
            ],
            "roles": [item.to_dict() for item in self.roles],
            "devices": {
                "playback_device": self.playback_device,
                "capture_device": self.capture_device,
                "capture_format": self.capture_format,
                "playback_format": self.playback_format,
                "sample_rate_hz": self.sample_rate_hz,
                "chunksize": self.chunksize,
                "target_level": self.target_level,
            },
            "limiter": {
                "type": "soft_clip",
                "threshold_dbfs": self.limiter_threshold_dbfs,
                "excluded_from_linear_transfer_P": True,
            },
            "stimulus_peak_ceiling_dbfs": self.stimulus_peak_ceiling_dbfs,
            "volume_ceiling_db": self.volume_ceiling_db,
            "graph_headroom_gain_db": self.graph_headroom_gain_db,
            "excluded_filter_layers": list(PROTECTED_NEUTRAL_GRAPH_EXCLUSIONS),
            "transport": {
                "alsa_device": CORRECTION_SUBSTREAM,
                "physical_program_channels": 2,
                "lane_semantics": {"0": "woofer", "1": "tweeter"},
                "summed_semantics": "sample_exact_identical_pcm_on_both_lanes",
            },
            "lifecycle": {
                "load": "inline_transient_under_existing_dsp_writer_lock",
                "restore_owner": "capture_entry_anchor",
                "restore": "exact_entry_config_on_success_abort_failure_or_restart",
                "clear": "only_after_confirmed_restore",
                "durable_profile_mutated": False,
                "undo_mutated": False,
            },
        }

    @property
    def fingerprint(self) -> str:
        return json_fingerprint(self.to_dict())

    def role(self, role: str) -> ProtectedRoleSpec:
        for item in self.roles:
            if item.role == role:
                return item
        raise ProtectedNeutralGraphError(f"protected graph has no role {role!r}")

    def sections_by_role(self) -> dict[str, tuple[LinearTransferSection, ...]]:
        return {
            role: tuple(item.linear_section() for item in self.role(role).filters)
            for role in ROLE_ORDER
        }

    def measurement_polarity_signs(self) -> dict[str, int]:
        return {role: self.role(role).physical_polarity_sign for role in ROLE_ORDER}


@dataclass(frozen=True)
class ProtectedNeutralSessionGraph:
    yaml_text: str
    identity: Mapping[str, Any]
    spec: ProtectedNeutralGraphSpec
    protection_by_role: Mapping[str, tuple[LinearTransferSection, ...]]
    configured_by_role: Mapping[str, tuple[LinearTransferSection, ...]]
    measurement_polarity_sign_by_role: Mapping[str, int]
    configured_polarity_sign_by_role: Mapping[str, int]
    limiter_threshold_dbfs: float
    volume_ceiling_db: float


def _profile_roles(
    component_safety_profile: Mapping[str, Any], *, sample_rate_hz: int
) -> tuple[ProtectedRoleSpec, ...]:
    raw_targets = component_safety_profile.get("targets")
    if not isinstance(raw_targets, list):
        raise ProtectedNeutralGraphError("component safety profile has no targets")
    by_role: dict[str, ProtectedRoleSpec] = {}
    for raw in raw_targets:
        if not isinstance(raw, Mapping) or raw.get("role") not in ROLE_ORDER:
            continue
        role = str(raw["role"])
        if role in by_role:
            raise ProtectedNeutralGraphError(f"component profile repeats {role}")
        target_id = str(raw.get("target_id") or "")
        target_fingerprint = raw.get("target_fingerprint")
        search = raw.get("crossover_search_band_hz")
        filters = raw.get("required_protection_filters")
        if (
            not target_id
            or not _is_sha256(target_fingerprint)
            or not isinstance(search, list)
            or len(search) != 2
            or not isinstance(filters, list)
        ):
            raise ProtectedNeutralGraphError(
                f"confirmed {role} declaration is incomplete"
            )
        lo = _finite(search[0], f"{role} crossover lower bound")
        hi = _finite(search[1], f"{role} crossover upper bound")
        if not 0.0 < lo <= hi:
            raise ProtectedNeutralGraphError(f"confirmed {role} bounds are invalid")
        polarity_raw = raw.get("physical_polarity")
        if polarity_raw is None:
            polarity_sign = 1
            polarity_source = "neutral_plus_one_no_declared_physical_fact"
        elif isinstance(polarity_raw, str) and polarity_raw in _POLARITY_VALUES:
            polarity_sign = _POLARITY_VALUES[str(polarity_raw)]
            polarity_source = "component_safety_profile.physical_polarity"
        else:
            raise ProtectedNeutralGraphError(
                f"confirmed {role} physical polarity cannot be represented"
            )
        normalized_filters = tuple(
            DeclaredProtectionFilter.from_mapping(item, sample_rate_hz=sample_rate_hz)
            for item in filters
        )
        if len({item.kind for item in normalized_filters}) != len(normalized_filters):
            raise ProtectedNeutralGraphError(f"confirmed {role} repeats a filter kind")
        if role == "tweeter" and not any(
            item.kind == "highpass" for item in normalized_filters
        ):
            raise ProtectedNeutralGraphError("confirmed tweeter high-pass is missing")
        by_role[role] = ProtectedRoleSpec(
            role=role,
            target_id=target_id,
            target_fingerprint=str(target_fingerprint),
            crossover_search_band_hz=(lo, hi),
            physical_polarity_sign=polarity_sign,
            physical_polarity_source=polarity_source,
            filters=normalized_filters,
        )
    if set(by_role) != set(ROLE_ORDER):
        raise ProtectedNeutralGraphError(
            "component safety profile must contain one woofer and one tweeter"
        )
    return tuple(by_role[role] for role in ROLE_ORDER)


def configured_transfer_from_preset(
    preset: Any,
) -> dict[str, tuple[LinearTransferSection, ...]]:
    """Use the emitted branch-chain owner for complete configured transfer C."""

    from jasper.active_speaker.branch_chain import sections_by_role

    source = sections_by_role(tuple(getattr(preset, "crossover_regions", ()) or ()))
    if set(source) != set(ROLE_ORDER) or any(not source[role] for role in ROLE_ORDER):
        raise ProtectedNeutralGraphError(
            "R15 requires one complete configured two-way crossover"
        )
    return {
        role: tuple(
            LinearTransferSection(
                highpass=item.highpass,
                frequency_hz=item.fc_hz,
                order=item.order,
                reason="configured_crossover_complete_transfer",
            )
            for item in source[role]
        )
        for role in ROLE_ORDER
    }


def protected_neutral_graph_identity(
    yaml_text: str, spec: ProtectedNeutralGraphSpec
) -> dict[str, Any]:
    encoded = yaml_text.encode("utf-8")
    active_raw_sha256 = hashlib.sha256(encoded).hexdigest()
    exact_state = ExactDspStateIdentity(
        {"active_raw_sha256": active_raw_sha256, "active_raw_bytes": len(encoded)}
    )
    core = {
        "schema_version": 1,
        "kind": PROTECTED_NEUTRAL_GRAPH_KIND,
        "graph_id": f"protected-neutral-{active_raw_sha256[:16]}",
        "active_raw_sha256": active_raw_sha256,
        "exact_dsp_state_identity": exact_state.to_dict(),
        "canonical_spec": spec.to_dict(),
        "canonical_spec_fingerprint": spec.fingerprint,
    }
    return {**core, "fingerprint": json_fingerprint(core)}


def build_protected_neutral_session_graph(
    *,
    preset: Any,
    component_safety_profile: Mapping[str, Any],
    role_channels: Mapping[str, int],
    playback_device: str,
    limiter_threshold_dbfs: float = -12.0,
    volume_ceiling_db: float = 0.0,
) -> ProtectedNeutralSessionGraph:
    """Build the exact two-lane graph once for a crossover session."""

    from jasper.active_speaker.camilla_yaml import (
        DEFAULT_CAPTURE_DEVICE,
        DEFAULT_CAPTURE_FORMAT,
        DEFAULT_PLAYBACK_FORMAT,
        DEFAULT_SAMPLE_RATE,
        configured_role_polarity_signs,
        emit_active_speaker_program_config,
        resolve_camilla_chunksize,
        resolve_camilla_target_level,
    )

    preset.validate()
    if preset.way_count != 2:
        raise ProtectedNeutralGraphError("protected-neutral v1 requires a 2-way preset")
    normalized_channels = {role: int(role_channels[role]) for role in ROLE_ORDER}
    if normalized_channels != {"woofer": 0, "tweeter": 1}:
        raise ProtectedNeutralGraphError(
            "correction_substream requires woofer lane 0 and tweeter lane 1"
        )
    profile_fingerprint = component_safety_profile.get("profile_fingerprint")
    if not _is_sha256(profile_fingerprint):
        raise ProtectedNeutralGraphError(
            "protected graph requires the confirmed component profile fingerprint"
        )
    limiter = _finite(limiter_threshold_dbfs, "limiter_threshold_dbfs")
    ceiling = _finite(volume_ceiling_db, "volume_ceiling_db")
    if not -120.0 <= limiter <= 0.0 or ceiling > 0.0:
        raise ProtectedNeutralGraphError("limiter or volume ceiling is unsafe")
    spec = ProtectedNeutralGraphSpec(
        profile_fingerprint=str(profile_fingerprint),
        preset_id=str(preset.preset_id),
        role_channels=normalized_channels,
        output_roles=tuple(
            (item.index, item.driver_role)
            for item in sorted(preset.channel_map.outputs, key=lambda item: item.index)
        ),
        roles=_profile_roles(
            component_safety_profile, sample_rate_hz=DEFAULT_SAMPLE_RATE
        ),
        playback_device=str(playback_device),
        capture_device=DEFAULT_CAPTURE_DEVICE,
        capture_format=DEFAULT_CAPTURE_FORMAT,
        playback_format=DEFAULT_PLAYBACK_FORMAT,
        sample_rate_hz=DEFAULT_SAMPLE_RATE,
        chunksize=resolve_camilla_chunksize(),
        target_level=resolve_camilla_target_level(),
        limiter_threshold_dbfs=limiter,
        stimulus_peak_ceiling_dbfs=AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
        volume_ceiling_db=ceiling,
    )
    yaml_text = emit_active_speaker_program_config(preset, graph_spec=spec)
    identity = protected_neutral_graph_identity(yaml_text, spec)
    return ProtectedNeutralSessionGraph(
        yaml_text=yaml_text,
        identity=identity,
        spec=spec,
        protection_by_role=spec.sections_by_role(),
        configured_by_role=configured_transfer_from_preset(preset),
        measurement_polarity_sign_by_role=spec.measurement_polarity_signs(),
        configured_polarity_sign_by_role=configured_role_polarity_signs(preset),
        limiter_threshold_dbfs=limiter,
        volume_ceiling_db=ceiling,
    )


def validate_protected_neutral_program_routing(
    program: Any, graph_identity: Mapping[str, Any]
) -> None:
    """Prove one pre-apply program fits the real stereo carrier."""

    spec = graph_identity.get("canonical_spec")
    if (
        graph_identity.get("kind") != PROTECTED_NEUTRAL_GRAPH_KIND
        or not isinstance(spec, Mapping)
        or graph_identity.get("canonical_spec_fingerprint")
        != json_fingerprint(spec)
        or spec.get("kind") != PROTECTED_NEUTRAL_SPEC_KIND
        or spec.get("role_channels") != {"woofer": 0, "tweeter": 1}
        or spec.get("transport", {}).get("physical_program_channels") != 2
        or int(getattr(program, "channels", 0)) != 2
    ):
        raise ProtectedNeutralGraphError("protected-neutral stereo identity mismatch")
    parsed_spec = ProtectedNeutralGraphSpec.from_dict(spec)
    for segment in program.stimulus_segments():
        gain_db = _finite(getattr(segment, "gain_db", None), "stimulus gain_db")
        effective_peak_dbfs = _finite(
            getattr(segment, "effective_peak_dbfs", None),
            "stimulus effective_peak_dbfs",
        )
        if (
            gain_db > parsed_spec.stimulus_peak_ceiling_dbfs
            or effective_peak_dbfs > parsed_spec.stimulus_peak_ceiling_dbfs
        ):
            raise ProtectedNeutralGraphError(
                "pre-apply stimulus exceeds the canonical conservative gain ceiling"
            )
        role = str(segment.role or "")
        if role == "summed":
            if segment.channel != 0:
                raise ProtectedNeutralGraphError(
                    "summed program must use the canonical mirrored stereo source"
                )
        elif segment.channel != {"woofer": 0, "tweeter": 1}.get(role):
            raise ProtectedNeutralGraphError(
                "isolated program stimulus is not on its protected physical lane"
            )
