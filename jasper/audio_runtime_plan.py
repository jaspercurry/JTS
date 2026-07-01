# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Explainable audio runtime plan.

This is the read-only "what should this box run?" layer for the audio knobs
that otherwise appear in several places: packaged systemd defaults, operator
env, generated reconciler env, hardware profile floors, and route policy.

Reconcilers still own the actual env-file writes. This module owns the decisions
those reconcilers consume, plus the diagnostics that let operator surfaces
explain the current intent.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, TypedDict, cast

from jasper.audio_hardware.dac import by_id as dac_profile_by_id
from jasper.audio_hardware.dac import latency_floor_for
from jasper.audio_runtime_overrides import (
    DEFAULT_AUDIO_RUNTIME_OVERRIDES_PATH,
    load_runtime_overrides,
    runtime_overrides_path,
)
from jasper.camilla_config_contract import (
    DEFAULT_CAPTURE_DEVICE,
    DEFAULT_CAPTURE_FORMAT,
    DEFAULT_CHUNKSIZE,
    DEFAULT_FILE_CAPTURE_RESAMPLER_PROFILE,
    DEFAULT_FILE_CAPTURE_RESAMPLER_TYPE,
    DEFAULT_LEAN_CAPTURE_FIFO,
    DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE_FORMAT,
    DEFAULT_PLAYBACK_DEVICE,
    DEFAULT_PLAYBACK_FORMAT,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_TARGET_LEVEL,
)
from jasper.env_load import read_env_file_state
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_LOOPBACK,
    COUPLING_TRANSPORT_PIPE,
    PIPE_PATH_ENV_VAR,
    OUTPUTD_PIPE_PATH_ENV_VAR,
    capture_kwargs_for_coupling,
    coupling_capture_kwargs_from_env,
    member_kwargs_are_pipe_sink,
    resolve_coupling,
    resolve_pipe_path,
    resolve_outputd_pipe_path,
)


DEFAULT_BASE_ENV_PATH = "/etc/jasper/jasper.env"
DEFAULT_OUTPUTD_ENV_PATH = "/var/lib/jasper/outputd.env"
DEFAULT_FANIN_ENV_PATH = "/var/lib/jasper/fanin.env"
DEFAULT_GROUPING_ENV_PATH = "/var/lib/jasper/grouping.env"

DEFAULT_OUTPUTD_PERIOD_FRAMES = 1024
DEFAULT_OUTPUTD_DAC_BUFFER_FRAMES = 3072
MAX_LOW_LATENCY_CORRECTION_GROUP_DELAY_FRAMES = 512
FANIN_INPUT_BUFFER_KEY = "JASPER_FANIN_INPUT_BUFFER_FRAMES"
FANIN_OUTPUT_BUFFER_KEY = "JASPER_FANIN_OUTPUT_BUFFER_FRAMES"
FANIN_ADAPTIVE_SHRUNK_FRAMES_ENV = "JASPER_FANIN_ADAPTIVE_SHRUNK_FRAMES"
DEFAULT_FANIN_INPUT_BUFFER_FRAMES = 4096
DEFAULT_FANIN_OUTPUT_BUFFER_FRAMES = 1024
MIN_FANIN_OUTPUT_BUFFER_FRAMES = 1024
USBSINK_OUTPUT_MODE_KEY = "JASPER_USBSINK_OUTPUT_MODE"
USBSINK_OUTPUT_MODE_ALOOP = "aloop"
USBSINK_OUTPUT_MODE_FIFO = "fifo"

OUTPUTD_LATENCY_KEYS = (
    "JASPER_CAMILLA_CHUNKSIZE",
    "JASPER_CAMILLA_TARGET_LEVEL",
    "JASPER_OUTPUTD_PERIOD_FRAMES",
    "JASPER_OUTPUTD_DAC_BUFFER_FRAMES",
)
AUDIO_RUNTIME_OVERRIDE_KEYS = frozenset(
    OUTPUTD_LATENCY_KEYS
    + (
        FANIN_INPUT_BUFFER_KEY,
        FANIN_OUTPUT_BUFFER_KEY,
    )
)

RouteMode = Literal[
    "solo",
    "active_leader",
    "active_follower",
    "invalid_grouping",
    "unknown",
]

SourceLowLatencyRoute = Literal["low_latency", "buffered"]

SourceKind = Literal[
    "operator_env",
    "generated_env",
    "device_profile",
    "packaged_default",
    "route_policy",
    "lab_override",
]

_VALID_ROUTE_MODES = {
    "solo",
    "active_leader",
    "active_follower",
    "invalid_grouping",
    "unknown",
}

_VALID_COUPLINGS = {COUPLING_LOOPBACK, COUPLING_TRANSPORT_PIPE}
_VALID_USBSINK_OUTPUT_MODES = {
    USBSINK_OUTPUT_MODE_ALOOP,
    USBSINK_OUTPUT_MODE_FIFO,
}

_ACTIVE_LEADER_TRANSPORT_PIPE_REASON = "fanin_transport_pipe_coupling_unsupported"
_ACTIVE_LEADER_TRANSPORT_PIPE_DETAIL = (
    "JASPER_FANIN_CAMILLA_COUPLING=transport_pipe is not supported while this box is an "
    "active multiroom leader; camilla#1's grouped program bake still captures "
    "the ALSA fan-in loopback. Keep the coupling on loopback until the grouped "
    "transport-pipe topology is designed."
)


class EmitSoundConfigKwargs(TypedDict, total=False):
    """Subset of ``emit_sound_config`` kwargs owned by runtime routing."""

    room_peqs_right: Any
    channel_delays_ms: Any
    channel_split: Any
    capture_pipe_path: str | None
    playback_pipe_path: str | None
    resampler_type: str | None
    resampler_profile: str | None
    enable_rate_adjust: bool
    transport_paced_pipe: bool


@dataclass(frozen=True)
class RuntimeSetting:
    """One resolved runtime knob with provenance and drift notes."""

    key: str
    value: int | str
    source_kind: SourceKind
    source: str
    unit: str = ""
    override_value: str | None = None
    generated_value: str | None = None
    operator_value: str | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key,
            "value": self.value,
            "source_kind": self.source_kind,
            "source": self.source,
        }
        if self.unit:
            out["unit"] = self.unit
        if self.override_value is not None:
            out["override_value"] = self.override_value
        if self.operator_value is not None:
            out["operator_value"] = self.operator_value
        if self.generated_value is not None:
            out["generated_value"] = self.generated_value
        if self.warnings:
            out["warnings"] = list(self.warnings)
        return out


@dataclass(frozen=True)
class CouplingSupport:
    """Route-policy verdict for one fan-in -> CamillaDSP coupling."""

    coupling: str
    route_mode: RouteMode
    supported: bool
    reason: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "coupling": self.coupling,
            "route_mode": self.route_mode,
            "supported": self.supported,
        }
        if self.reason:
            out["reason"] = self.reason
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass(frozen=True)
class RuntimeEnvAction:
    """One reconciler env-file action decided by the runtime plan."""

    action: Literal["set", "unset"]
    key: str
    value: str = ""

    def to_dict(self) -> dict[str, str]:
        out = {"action": self.action, "key": self.key}
        if self.action == "set":
            out["value"] = self.value
        return out


@dataclass(frozen=True)
class FaninOutputBufferTarget:
    """Resolved adaptive fan-in output-buffer target."""

    frames: int
    warning_event: str = ""
    detail: str = ""
    raw_value: str = ""


@dataclass(frozen=True)
class SourceRouteDecision:
    """Pure source-route verdict for optional low-latency consumers.

    The route decision is intentionally independent of the concrete mechanism
    that consumes it. Today two experimental consumers use it:
    ``JASPER_LEAN_LANE`` swaps CamillaDSP to the USB lean FIFO, and
    ``JASPER_FANIN_ADAPTIVE_BUFFER`` shrinks fan-in's output buffer. A future
    single low-latency source route should change this support matrix/consumer
    set here rather than re-implementing the same source-exclusivity check in
    each caller.
    """

    route: SourceLowLatencyRoute
    reason: str
    active_sources: tuple[str, ...] = ()
    winner: str | None = None


@dataclass(frozen=True)
class TransportTopology:
    """Resolved audio transport topology for status/doctor surfaces."""

    name: str
    fanin_to_camilla: Mapping[str, Any]
    camilla_to_outputd: Mapping[str, Any]
    camilla: Mapping[str, Any]
    outputd_content_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "fanin_to_camilla": dict(self.fanin_to_camilla),
            "camilla_to_outputd": dict(self.camilla_to_outputd),
            "camilla": dict(self.camilla),
            "outputd_content_source": self.outputd_content_source,
        }


@dataclass(frozen=True)
class CorrectionLatencyEligibility:
    """Whether the loaded/generated correction shape may claim low latency."""

    eligible: bool
    minimum_phase_or_iir: bool
    measured_group_delay_frames: int | None = 0
    blocking_reason: str = ""
    mode: str = "peq_iir"
    max_group_delay_frames: int = MAX_LOW_LATENCY_CORRECTION_GROUP_DELAY_FRAMES

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "eligible": self.eligible,
            "minimum_phase_or_iir": self.minimum_phase_or_iir,
            "measured_group_delay_frames": self.measured_group_delay_frames,
            "mode": self.mode,
            "max_group_delay_frames": self.max_group_delay_frames,
        }
        if self.blocking_reason:
            out["blocking_reason"] = self.blocking_reason
        return out


@dataclass(frozen=True)
class LowLatencyFeatureFlags:
    """Parsed opt-in gates for experimental low-latency consumers."""

    lean_lane: bool
    adaptive_buffer: bool


@dataclass(frozen=True)
class AudioRuntimePlan:
    """Resolved audio settings plus route-policy errors."""

    profile_id: str
    profile_label: str
    route_mode: RouteMode
    settings: tuple[RuntimeSetting, ...]
    coupling_support: CouplingSupport
    transport_topology: TransportTopology
    correction_latency_eligibility: CorrectionLatencyEligibility
    plan_warnings: tuple[str, ...] = ()

    def setting(self, key: str) -> RuntimeSetting:
        for setting in self.settings:
            if setting.key == key:
                return setting
        raise KeyError(key)

    @property
    def warnings(self) -> tuple[str, ...]:
        out: list[str] = []
        out.extend(self.plan_warnings)
        for setting in self.settings:
            out.extend(setting.warnings)
        return tuple(out)

    @property
    def errors(self) -> tuple[str, ...]:
        out: list[str] = []
        if not self.coupling_support.supported:
            out.append(self.coupling_support.detail)
        try:
            coupling = str(self.setting(COUPLING_ENV_VAR).value)
        except KeyError:
            coupling = COUPLING_LOOPBACK
        if (
            coupling == COUPLING_TRANSPORT_PIPE
            and not self.correction_latency_eligibility.eligible
        ):
            out.append(
                "transport_pipe low-latency mode is blocked by correction "
                f"latency: {self.correction_latency_eligibility.blocking_reason}"
            )
        return tuple(out)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "profile_label": self.profile_label,
            "route_mode": self.route_mode,
            "settings": [setting.to_dict() for setting in self.settings],
            "coupling_support": self.coupling_support.to_dict(),
            "transport_topology": self.transport_topology.to_dict(),
            "correction_latency_eligibility": (
                self.correction_latency_eligibility.to_dict()
            ),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


def coupling_supported_for_route(coupling: str, route_mode: RouteMode) -> CouplingSupport:
    """Return whether ``coupling`` is supported for ``route_mode``.

    Today the only blocked combination is active-leader + ``transport_pipe``.
    Keeping that in a route-policy function makes grouped transport-pipe support
    a deliberate support-matrix change instead of another scattered conditional.
    """

    normalized = resolve_coupling(coupling)
    mode = route_mode if route_mode in _VALID_ROUTE_MODES else "unknown"
    if normalized == COUPLING_TRANSPORT_PIPE and mode == "active_leader":
        return CouplingSupport(
            coupling=normalized,
            route_mode=mode,  # type: ignore[arg-type]
            supported=False,
            reason=_ACTIVE_LEADER_TRANSPORT_PIPE_REASON,
            detail=_ACTIVE_LEADER_TRANSPORT_PIPE_DETAIL,
        )
    return CouplingSupport(
        coupling=normalized,
        route_mode=mode,  # type: ignore[arg-type]
        supported=True,
    )


def fanin_coupling_action(
    desired_raw: str | None,
    route_mode: RouteMode,
) -> tuple[RuntimeEnvAction | None, CouplingSupport]:
    """Return the ``fanin.env`` coupling action and route-policy verdict."""

    desired = resolve_coupling(desired_raw)
    support = coupling_supported_for_route(desired, route_mode)
    if not support.supported:
        return None, support
    return RuntimeEnvAction("set", COUPLING_ENV_VAR, desired), support


def route_mode_from_grouping_config(cfg: Any) -> RouteMode:
    """Classify the multiroom route shape from a ``GroupingConfig``-like object."""

    if not bool(getattr(cfg, "enabled", False)):
        return "solo"
    if getattr(cfg, "error", None):
        return "invalid_grouping"
    role = str(getattr(cfg, "role", "") or "").strip()
    if role == "leader":
        return "active_leader"
    if role == "follower":
        return "active_follower"
    return "unknown"


def decide_source_low_latency_route(
    *,
    active_sources: tuple[Any, ...] | list[Any],
    winner: Any | None,
    enabled: bool,
    exclusive_source: str = "usbsink",
) -> SourceRouteDecision:
    """Pure source-route policy for optional low-latency audio consumers.

    ``"low_latency"`` iff the feature flag is enabled, exactly one source is
    active, that source is ``exclusive_source``, and it is also the audible
    winner. Everything else stays on the safe buffered route.

    ``active_sources`` / ``winner`` may be :class:`str` or enum-like values with
    a ``.value`` attribute; the decision normalizes them to stable source ids in
    the returned object. No I/O, no daemon state, no CamillaDSP calls.
    """

    active = tuple(_source_id(source) for source in active_sources)
    winner_id = _source_id(winner) if winner is not None else None
    target = str(exclusive_source).strip()
    if not enabled:
        return SourceRouteDecision("buffered", "flag_off", active, winner_id)
    if not active:
        return SourceRouteDecision("buffered", "idle", active, winner_id)
    if active != (target,):
        return SourceRouteDecision("buffered", "not_exclusive", active, winner_id)
    if winner_id != target:
        return SourceRouteDecision("buffered", "non_usb_winner", active, winner_id)
    return SourceRouteDecision("low_latency", "usb_exclusive", active, winner_id)


def low_latency_feature_flags(
    env: Mapping[str, str] | None = None,
) -> LowLatencyFeatureFlags:
    """Return default-off low-latency feature gates from ``env``.

    These flags intentionally share one parser because they consume the same
    source-route policy. Only the exact literal ``enabled`` (case-insensitive,
    stripped) turns either experiment on; unset, ``disabled``, ``1``, and
    ``true`` all stay off.
    """

    if env is None:
        env = os.environ
    return LowLatencyFeatureFlags(
        lean_lane=_enabled_literal(env.get("JASPER_LEAN_LANE")),
        adaptive_buffer=_enabled_literal(env.get("JASPER_FANIN_ADAPTIVE_BUFFER")),
    )


def _source_id(source: Any) -> str:
    value = getattr(source, "value", source)
    return str(value).strip()


def _enabled_literal(raw: str | None) -> bool:
    return (raw or "").strip().lower() == "enabled"


def build_audio_runtime_plan_from_system(
    *,
    base_env_path: str = DEFAULT_BASE_ENV_PATH,
    outputd_env_path: str = DEFAULT_OUTPUTD_ENV_PATH,
    fanin_env_path: str = DEFAULT_FANIN_ENV_PATH,
    grouping_env_path: str = DEFAULT_GROUPING_ENV_PATH,
    overrides_path: str | None = None,
    output_hardware_state_path: str | None = None,
) -> AudioRuntimePlan:
    """Build the plan from the same persistent files the daemons load."""

    base = read_env_file_state(base_env_path)
    outputd = read_env_file_state(outputd_env_path)
    fanin = read_env_file_state(fanin_env_path)
    resolved_overrides_path = overrides_path or runtime_overrides_path()
    overrides = load_runtime_overrides(
        resolved_overrides_path,
        allowed_keys=AUDIO_RUNTIME_OVERRIDE_KEYS,
    )
    profile_id = ""
    try:
        from jasper.output_hardware import load_state

        state = load_state(output_hardware_state_path)
        if state is not None:
            profile_id = state.profile_id
    except ImportError:
        profile_id = ""
    route_mode: RouteMode = "unknown"
    try:
        from jasper.multiroom.config import load_config

        route_mode = route_mode_from_grouping_config(load_config(grouping_env_path))
    except ImportError:
        route_mode = "unknown"
    correction_config_path = _active_camilla_config_path_from_statefile()
    return build_audio_runtime_plan(
        base_env=base.values,
        outputd_env=outputd.values,
        fanin_env=fanin.values,
        overrides=overrides.values(),
        profile_id=profile_id,
        route_mode=route_mode,
        correction_config_path=correction_config_path,
        base_env_label=base.path,
        outputd_env_label=outputd.path,
        fanin_env_label=fanin.path,
        override_label=resolved_overrides_path,
        plan_warnings=overrides.warnings,
    )


def build_audio_runtime_plan(
    *,
    base_env: Mapping[str, str] | None = None,
    outputd_env: Mapping[str, str] | None = None,
    fanin_env: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
    profile_id: str | None = None,
    route_mode: RouteMode = "unknown",
    base_env_label: str = DEFAULT_BASE_ENV_PATH,
    outputd_env_label: str = DEFAULT_OUTPUTD_ENV_PATH,
    fanin_env_label: str = DEFAULT_FANIN_ENV_PATH,
    override_label: str = DEFAULT_AUDIO_RUNTIME_OVERRIDES_PATH,
    plan_warnings: tuple[str, ...] = (),
    correction_config_path: str | None = None,
) -> AudioRuntimePlan:
    """Resolve audio knobs from operator env, generated env, profile, defaults."""

    base_values = dict(base_env or {})
    outputd_values = dict(outputd_env or {})
    fanin_values = dict(fanin_env or {})
    override_values = dict(overrides or {})
    profile_id = (profile_id or "").strip()
    profile = dac_profile_by_id(profile_id) if profile_id else None
    floor = latency_floor_for(profile_id) if profile_id else None

    settings = [
        _resolve_profile_floor_int(
            key="JASPER_CAMILLA_CHUNKSIZE",
            default=DEFAULT_CHUNKSIZE,
            floor_value=getattr(floor, "camilla_chunksize", None),
            base_env=base_values,
            override_env=override_values,
            generated_env=outputd_values,
            base_label=base_env_label,
            override_label=override_label,
            generated_label=outputd_env_label,
            profile_id=profile_id,
        ),
        _resolve_profile_floor_int(
            key="JASPER_CAMILLA_TARGET_LEVEL",
            default=DEFAULT_TARGET_LEVEL,
            floor_value=getattr(floor, "camilla_target_level", None),
            base_env=base_values,
            override_env=override_values,
            generated_env=outputd_values,
            base_label=base_env_label,
            override_label=override_label,
            generated_label=outputd_env_label,
            profile_id=profile_id,
        ),
        _resolve_profile_floor_int(
            key="JASPER_OUTPUTD_PERIOD_FRAMES",
            default=DEFAULT_OUTPUTD_PERIOD_FRAMES,
            floor_value=getattr(floor, "outputd_period_frames", None),
            base_env=base_values,
            override_env=override_values,
            generated_env=outputd_values,
            base_label=base_env_label,
            override_label=override_label,
            generated_label=outputd_env_label,
            profile_id=profile_id,
        ),
        _resolve_profile_floor_int(
            key="JASPER_OUTPUTD_DAC_BUFFER_FRAMES",
            default=DEFAULT_OUTPUTD_DAC_BUFFER_FRAMES,
            floor_value=getattr(floor, "outputd_dac_buffer_frames", None),
            base_env=base_values,
            override_env=override_values,
            generated_env=outputd_values,
            base_label=base_env_label,
            override_label=override_label,
            generated_label=outputd_env_label,
            profile_id=profile_id,
        ),
        _resolve_fanin_int(
            key=FANIN_INPUT_BUFFER_KEY,
            default=DEFAULT_FANIN_INPUT_BUFFER_FRAMES,
            base_env=base_values,
            override_env=override_values,
            fanin_env=fanin_values,
            base_label=base_env_label,
            override_label=override_label,
            fanin_label=fanin_env_label,
        ),
        _resolve_fanin_int(
            key=FANIN_OUTPUT_BUFFER_KEY,
            default=DEFAULT_FANIN_OUTPUT_BUFFER_FRAMES,
            base_env=base_values,
            override_env=override_values,
            fanin_env=fanin_values,
            base_label=base_env_label,
            override_label=override_label,
            fanin_label=fanin_env_label,
        ),
    ]
    coupling_setting = _resolve_coupling(
        base_env=base_values,
        override_env=override_values,
        fanin_env=fanin_values,
        base_label=base_env_label,
        override_label=override_label,
        fanin_label=fanin_env_label,
    )
    settings.append(coupling_setting)
    support = coupling_supported_for_route(str(coupling_setting.value), route_mode)
    topology = transport_topology_for_coupling(
        str(coupling_setting.value),
        fanin_env=fanin_values,
        outputd_env=outputd_values,
    )
    combined_plan_warnings = tuple(plan_warnings) + _transport_pipe_env_warnings(
        coupling=str(coupling_setting.value),
        outputd_env=outputd_values,
        outputd_env_label=outputd_env_label,
    )
    return AudioRuntimePlan(
        profile_id=profile_id or "unknown",
        profile_label=profile.label if profile is not None else "unknown",
        route_mode=route_mode if route_mode in _VALID_ROUTE_MODES else "unknown",
        settings=tuple(settings),
        coupling_support=support,
        transport_topology=topology,
        correction_latency_eligibility=correction_latency_eligibility_for_config(
            correction_config_path
        ),
        plan_warnings=combined_plan_warnings,
    )


def transport_topology_for_coupling(
    coupling: str | None,
    *,
    fanin_env: Mapping[str, str] | None = None,
    outputd_env: Mapping[str, str] | None = None,
) -> TransportTopology:
    """Return the concrete transport topology implied by the coupling intent."""

    fanin_values = dict(fanin_env or {})
    outputd_values = dict(outputd_env or {})
    normalized = resolve_coupling(coupling)
    if normalized == COUPLING_TRANSPORT_PIPE:
        capture_pipe = resolve_pipe_path(fanin_values.get(PIPE_PATH_ENV_VAR))
        output_pipe = resolve_outputd_pipe_path(
            outputd_values.get(OUTPUTD_PIPE_PATH_ENV_VAR)
        )
        return TransportTopology(
            name=COUPLING_TRANSPORT_PIPE,
            fanin_to_camilla={
                "transport": "pipe",
                "path": capture_pipe,
                "writer": "jasper-fanin",
                "camilla_capture_type": "RawFile",
                "format": DEFAULT_CAPTURE_FORMAT,
                "channels": 2,
                "sample_rate": DEFAULT_SAMPLE_RATE,
            },
            camilla_to_outputd={
                "transport": "local_pipe",
                "path": output_pipe,
                "camilla_playback_type": "File",
                "reader": "jasper-outputd",
                "format": DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE_FORMAT,
                "channels": 2,
                "sample_rate": DEFAULT_SAMPLE_RATE,
            },
            camilla={
                "enable_rate_adjust": False,
                "capture_resampler": None,
            },
            outputd_content_source="local_pipe",
        )
    return TransportTopology(
        name=COUPLING_LOOPBACK,
        fanin_to_camilla={
            "transport": "alsa_loopback",
            "writer": "jasper-fanin",
            "playback_pcm": "hw:Loopback,0,7",
            "camilla_capture_device": DEFAULT_CAPTURE_DEVICE,
            "format": DEFAULT_CAPTURE_FORMAT,
            "channels": 2,
            "sample_rate": DEFAULT_SAMPLE_RATE,
        },
        camilla_to_outputd={
            "transport": "alsa_loopback",
            "camilla_playback_device": DEFAULT_PLAYBACK_DEVICE,
            "outputd_capture_pcm": "outputd_content_capture",
            "format": DEFAULT_PLAYBACK_FORMAT,
            "channels": 2,
            "sample_rate": DEFAULT_SAMPLE_RATE,
        },
        camilla={
            "enable_rate_adjust": True,
            "capture_resampler": None,
        },
        outputd_content_source="alsa",
    )


def _transport_pipe_env_warnings(
    *,
    coupling: str,
    outputd_env: Mapping[str, str],
    outputd_env_label: str,
) -> tuple[str, ...]:
    """Return warnings when outputd's generated env contradicts coupling intent."""

    raw_outputd_pipe = str(outputd_env.get(OUTPUTD_PIPE_PATH_ENV_VAR, "")).strip()
    if coupling == COUPLING_TRANSPORT_PIPE:
        if raw_outputd_pipe:
            return ()
        return (
            f"{COUPLING_ENV_VAR}=transport_pipe requires "
            f"{OUTPUTD_PIPE_PATH_ENV_VAR} in {outputd_env_label}; "
            "run jasper-fanin-coupling-reconcile transport_pipe so outputd "
            "reads the same Camilla playback pipe the graph emits",
        )
    if raw_outputd_pipe:
        return (
            f"stale {OUTPUTD_PIPE_PATH_ENV_VAR} in {outputd_env_label} while "
            f"{COUPLING_ENV_VAR}={coupling}; run "
            "jasper-fanin-coupling-reconcile loopback to return outputd to ALSA "
            "content capture",
        )
    return ()


def correction_latency_eligibility(
    *,
    fir_mode: str | None = None,
    measured_group_delay_ms: float | None = None,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    max_group_delay_frames: int = MAX_LOW_LATENCY_CORRECTION_GROUP_DELAY_FRAMES,
) -> CorrectionLatencyEligibility:
    """Return the hard gate for claiming low-latency room correction.

    PEQ/IIR or minimum-phase correction is eligible. Linear, mixed, or unknown
    FIR is eligible only when measured group delay is present and inside the
    budget; otherwise the system may still play, but it must not claim the
    low-latency target.
    """

    mode = (fir_mode or "peq_iir").strip().lower()
    if mode in {"", "peq", "iir", "peq_iir", "minimum_phase"}:
        return CorrectionLatencyEligibility(
            eligible=True,
            minimum_phase_or_iir=True,
            measured_group_delay_frames=0,
            mode="minimum_phase" if mode == "minimum_phase" else "peq_iir",
            max_group_delay_frames=max_group_delay_frames,
        )
    delay_frames: int | None = None
    if measured_group_delay_ms is not None:
        delay_frames = round(float(measured_group_delay_ms) * sample_rate / 1000.0)
    if delay_frames is None:
        return CorrectionLatencyEligibility(
            eligible=False,
            minimum_phase_or_iir=False,
            measured_group_delay_frames=None,
            blocking_reason="fir_group_delay_unmeasured",
            mode=mode,
            max_group_delay_frames=max_group_delay_frames,
        )
    if delay_frames > max_group_delay_frames:
        return CorrectionLatencyEligibility(
            eligible=False,
            minimum_phase_or_iir=False,
            measured_group_delay_frames=delay_frames,
            blocking_reason="fir_group_delay_exceeds_low_latency_budget",
            mode=mode,
            max_group_delay_frames=max_group_delay_frames,
        )
    return CorrectionLatencyEligibility(
        eligible=True,
        minimum_phase_or_iir=False,
        measured_group_delay_frames=delay_frames,
        mode=mode,
        max_group_delay_frames=max_group_delay_frames,
    )


_CONV_FILTER_RE = re.compile(
    r"^\s*type:\s*(?:Conv|Convolution)\s*$",
    flags=re.IGNORECASE | re.MULTILINE,
)
_WAV_FILENAME_RE = re.compile(
    r"^\s*filename:\s*[\"']?([^\"'\n#]+?\.wav)[\"']?\s*$",
    flags=re.IGNORECASE | re.MULTILINE,
)


def correction_latency_eligibility_for_config(
    config_path: str | None,
) -> CorrectionLatencyEligibility:
    """Read the active Camilla config for FIR latency evidence.

    PEQ/IIR configs have no convolution filter and remain eligible. A config
    with convolution filters must carry bundle-local FIR metadata beside each
    referenced coefficient WAV; missing/unknown metadata blocks the
    low-latency claim instead of silently assuming minimum phase.
    """

    if not config_path:
        return correction_latency_eligibility()
    path = Path(config_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return correction_latency_eligibility()
    if not _CONV_FILTER_RE.search(text):
        return correction_latency_eligibility()

    metadata_paths = _fir_metadata_paths_for_config(text, config_path=path)
    if not metadata_paths:
        return correction_latency_eligibility(fir_mode="unknown")

    verdicts: list[CorrectionLatencyEligibility] = []
    for metadata_path in metadata_paths:
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return correction_latency_eligibility(fir_mode="unknown")
        if not isinstance(payload, dict):
            return correction_latency_eligibility(fir_mode="unknown")
        mode = str(payload.get("mode") or "unknown")
        delay_raw = payload.get("filter_group_delay_ms")
        delay_ms: float | None
        try:
            delay_ms = float(delay_raw) if delay_raw is not None else None
        except (TypeError, ValueError):
            delay_ms = None
        verdict = correction_latency_eligibility(
            fir_mode=mode,
            measured_group_delay_ms=delay_ms,
        )
        if not verdict.eligible:
            return verdict
        verdicts.append(verdict)

    non_min_phase = [v for v in verdicts if not v.minimum_phase_or_iir]
    if not non_min_phase:
        return correction_latency_eligibility(fir_mode="minimum_phase")
    worst = max(
        non_min_phase,
        key=lambda v: v.measured_group_delay_frames or 0,
    )
    return CorrectionLatencyEligibility(
        eligible=True,
        minimum_phase_or_iir=False,
        measured_group_delay_frames=worst.measured_group_delay_frames,
        blocking_reason="",
        mode="fir_measured",
        max_group_delay_frames=worst.max_group_delay_frames,
    )


def _fir_metadata_paths_for_config(text: str, *, config_path: Path) -> tuple[Path, ...]:
    out: list[Path] = []
    for raw in _WAV_FILENAME_RE.findall(text):
        wav_path = Path(raw.strip())
        if not wav_path.is_absolute():
            wav_path = config_path.parent / wav_path
        out.append(wav_path.with_suffix(".json"))
    return tuple(out)


def _active_camilla_config_path_from_statefile() -> str | None:
    statefile = Path(
        os.environ.get(
            "JASPER_CAMILLA_STATEFILE",
            "/var/lib/camilladsp/outputd-statefile.yml",
        )
    )
    try:
        text = statefile.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^\s*config_path:\s*(.+?)\s*$", text, flags=re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip().strip("'\"") or None


def outputd_latency_floor_actions(
    *,
    profile_id: str | None,
    base_env: Mapping[str, str] | None = None,
    outputd_env: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> tuple[RuntimeEnvAction, ...]:
    """Return outputd.env actions for the active DAC latency floor.

    This is the writer-side single source of truth for the audio-hardware
    reconciler's latency-floor env changes:

    - an operator key in ``jasper.env`` wins by REMOVING the generated key from
      ``outputd.env``;
    - a DAC profile floor writes the generated value;
    - no floor (or no recognized profile) REMOVES stale generated values so the
      packaged defaults apply.

    ``outputd_env`` is accepted for API symmetry with the explain plan and future
    no-op/skipped action detail; the current bash writer remains responsible for
    deciding whether a set/unset changes the file.
    """

    del outputd_env
    base_values = dict(base_env or {})
    override_values = dict(overrides or {})
    profile = (profile_id or "").strip()
    floor = latency_floor_for(profile) if profile else None
    floor_values: dict[str, int] = {}
    if floor is not None:
        floor_values = {
            "JASPER_CAMILLA_CHUNKSIZE": floor.camilla_chunksize,
            "JASPER_CAMILLA_TARGET_LEVEL": floor.camilla_target_level,
            "JASPER_OUTPUTD_PERIOD_FRAMES": floor.outputd_period_frames,
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES": floor.outputd_dac_buffer_frames,
        }

    actions: list[RuntimeEnvAction] = []
    for key in OUTPUTD_LATENCY_KEYS:
        override_value, _ = _positive_int(_raw(override_values, key))
        if override_value is not None:
            actions.append(RuntimeEnvAction("set", key, str(override_value)))
        elif key in base_values or key not in floor_values:
            actions.append(RuntimeEnvAction("unset", key))
        else:
            actions.append(RuntimeEnvAction("set", key, str(floor_values[key])))
    return tuple(actions)


def fanin_output_buffer_action(frames: int | None) -> RuntimeEnvAction:
    """Return the ``fanin.env`` action for the output-buffer override.

    ``frames=None`` means restore the packaged default by removing the override.
    A concrete value must meet the production floor; below-floor lab values are
    rejected before any reconciler writes them into persistent config.
    """

    if frames is None:
        return RuntimeEnvAction("unset", FANIN_OUTPUT_BUFFER_KEY)
    if frames < MIN_FANIN_OUTPUT_BUFFER_FRAMES:
        raise ValueError(f"{frames} below floor {MIN_FANIN_OUTPUT_BUFFER_FRAMES}")
    return RuntimeEnvAction("set", FANIN_OUTPUT_BUFFER_KEY, str(frames))


def usbsink_output_mode_action(mode: str) -> RuntimeEnvAction:
    """Return the ``usbsink.env`` action for the USB bridge output mode."""

    normalized = str(mode).strip().lower()
    if normalized not in _VALID_USBSINK_OUTPUT_MODES:
        raise ValueError(f"invalid usbsink output mode {mode!r}")
    return RuntimeEnvAction("set", USBSINK_OUTPUT_MODE_KEY, normalized)


def lean_capture_kwargs(capture_pipe_path: str | None = None) -> EmitSoundConfigKwargs:
    """Return CamillaDSP RawFile-capture kwargs for the USB-only lean lane."""

    return {
        "capture_pipe_path": capture_pipe_path or DEFAULT_LEAN_CAPTURE_FIFO,
        "resampler_type": DEFAULT_FILE_CAPTURE_RESAMPLER_TYPE,
        "resampler_profile": DEFAULT_FILE_CAPTURE_RESAMPLER_PROFILE,
        "enable_rate_adjust": True,
    }


def fanin_coupling_capture_kwargs(
    coupling: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> EmitSoundConfigKwargs:
    """Return CamillaDSP capture kwargs for the shared fan-in coupling.

    ``coupling=None`` means read the live env, matching ordinary sound/correction
    emits. An explicit coupling is used by the coupling reconciler immediately
    after it rewrites ``fanin.env``; process env may still be stale, so the
    explicit value wins while the FIFO path still comes from the supplied/live
    env.
    """

    if coupling is None:
        return cast(
            EmitSoundConfigKwargs,
            coupling_capture_kwargs_from_env(dict(os.environ if env is None else env)),
        )
    source = os.environ if env is None else env
    return cast(
        EmitSoundConfigKwargs,
        capture_kwargs_for_coupling(
            coupling,
            pipe_path=resolve_pipe_path(source.get(PIPE_PATH_ENV_VAR)),
            outputd_pipe_path=resolve_outputd_pipe_path(
                source.get(OUTPUTD_PIPE_PATH_ENV_VAR)
            ),
        ),
    )


def apply_capture_precedence(
    emit_kwargs: Mapping[str, object],
    fanin_coupling_capture_kwargs: Mapping[str, object] | None,
    *,
    lean_capture_kwargs: Mapping[str, object] | None,
    member_kwargs: Mapping[str, object] | None,
) -> EmitSoundConfigKwargs:
    """Apply capture-precedence policy to an ``emit_sound_config`` kwargs dict.

    The shared fan-in transport coupling applies only when no more-specific
    topology already owns this emit:

    - a live lean capture wins because it is the more-exclusive USB-only path;
    - a grouped/member pipe sink wins because its Snapcast playback pipe is
      mutually exclusive with the local Camilla -> outputd playback pipe;
    - otherwise the fan-in coupling kwargs overwrite the local Camilla capture
      and playback transport keys together.

    Empty coupling kwargs keep the input kwargs unchanged apart from returning a
    detached ``dict`` for callers to mutate safely.
    """

    if (
        not fanin_coupling_capture_kwargs
        or lean_capture_kwargs
        or member_kwargs_are_pipe_sink(dict(member_kwargs or {}))
    ):
        return cast(EmitSoundConfigKwargs, dict(emit_kwargs))
    merged = dict(emit_kwargs)
    merged.update(fanin_coupling_capture_kwargs)
    return cast(EmitSoundConfigKwargs, merged)


def resolve_fanin_output_buffer_target(
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> FaninOutputBufferTarget:
    """Resolve the adaptive fan-in output-buffer target from lab override env."""

    values = dict(env or {})
    override_raw = _raw(dict(overrides or {}), FANIN_OUTPUT_BUFFER_KEY)
    raw = (
        override_raw
        if override_raw is not None
        else str(values.get(FANIN_ADAPTIVE_SHRUNK_FRAMES_ENV, "")).strip()
    )
    if not raw:
        return FaninOutputBufferTarget(MIN_FANIN_OUTPUT_BUFFER_FRAMES)
    try:
        value = int(raw)
    except ValueError:
        return FaninOutputBufferTarget(
            MIN_FANIN_OUTPUT_BUFFER_FRAMES,
            warning_event="fanin.adaptive_shrunk_frames_invalid",
            detail="not an integer",
            raw_value=raw,
        )
    if value < MIN_FANIN_OUTPUT_BUFFER_FRAMES:
        return FaninOutputBufferTarget(
            MIN_FANIN_OUTPUT_BUFFER_FRAMES,
            warning_event="fanin.adaptive_shrunk_frames_below_floor",
            detail=f"below floor {MIN_FANIN_OUTPUT_BUFFER_FRAMES}",
            raw_value=raw,
        )
    return FaninOutputBufferTarget(value, raw_value=raw)


def _positive_int(raw: str | None) -> tuple[int | None, str | None]:
    if raw is None:
        return None, None
    text = str(raw).strip().strip("'\"")
    if not text:
        return None, "empty"
    try:
        value = int(text)
    except ValueError:
        return None, "not an integer"
    if value <= 0:
        return None, "must be > 0"
    return value, None


def _raw(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    if value is None:
        return None
    return str(value).strip().strip("'\"")


def _resolve_profile_floor_int(
    *,
    key: str,
    default: int,
    floor_value: int | None,
    base_env: Mapping[str, str],
    override_env: Mapping[str, str],
    generated_env: Mapping[str, str],
    base_label: str,
    override_label: str,
    generated_label: str,
    profile_id: str,
) -> RuntimeSetting:
    operator_raw = _raw(base_env, key)
    override_raw = _raw(override_env, key)
    generated_raw = _raw(generated_env, key)
    operator_value, operator_error = _positive_int(operator_raw)
    override_value, override_error = _positive_int(override_raw)
    generated_value, generated_error = _positive_int(generated_raw)
    warnings: list[str] = []

    if override_error is not None:
        warnings.append(
            f"{key} in {override_label} is invalid ({override_raw!r}: "
            f"{override_error}); ignored"
        )
    if operator_error is not None:
        warnings.append(
            f"{key} in {base_label} is invalid ({operator_raw!r}: "
            f"{operator_error}); ignored"
        )
    if generated_error is not None:
        warnings.append(
            f"{key} in {generated_label} is invalid ({generated_raw!r}: "
            f"{generated_error}); remove it or rerun audio hardware reconcile"
        )
    if operator_raw is not None and generated_raw is not None:
        warnings.append(
            f"{key} is set in both {base_label} and {generated_label}; "
            "one knob has two homes"
        )
    if override_raw is not None and (operator_raw is not None or generated_raw is not None):
        warnings.append(
            f"{key} lab override in {override_label} is active; it intentionally "
            "wins over env/profile values"
        )

    if override_value is not None:
        return RuntimeSetting(
            key=key,
            value=override_value,
            source_kind="lab_override",
            source=override_label,
            unit="frames",
            override_value=override_raw,
            operator_value=operator_raw,
            generated_value=generated_raw,
            warnings=tuple(warnings),
        )

    if operator_value is not None:
        return RuntimeSetting(
            key=key,
            value=operator_value,
            source_kind="operator_env",
            source=base_label,
            unit="frames",
            operator_value=operator_raw,
            generated_value=generated_raw,
            warnings=tuple(warnings),
        )

    if floor_value is not None:
        if generated_value is None:
            warnings.append(
                f"{key} profile floor for {profile_id} is {floor_value}, but "
                f"{generated_label} does not emit it; run "
                "jasper-audio-hardware-reconcile"
            )
        elif generated_value != floor_value:
            warnings.append(
                f"{key} in {generated_label} is {generated_value}, but the "
                f"{profile_id} profile floor is {floor_value}; rerun "
                "audio hardware reconcile"
            )
        return RuntimeSetting(
            key=key,
            value=floor_value,
            source_kind="device_profile",
            source=f"DacProfile:{profile_id}",
            unit="frames",
            operator_value=operator_raw,
            generated_value=generated_raw,
            warnings=tuple(warnings),
        )

    if generated_value is not None and generated_value != default:
        warnings.append(
            f"{key} in {generated_label} is {generated_value}, but the active "
            f"profile has no floor; stale generated value will override the "
            f"packaged default {default}"
        )
    return RuntimeSetting(
        key=key,
        value=default,
        source_kind="packaged_default",
        source="packaged systemd/Camilla default",
        unit="frames",
        operator_value=operator_raw,
        generated_value=generated_raw,
        warnings=tuple(warnings),
    )


def _resolve_fanin_int(
    *,
    key: str,
    default: int,
    base_env: Mapping[str, str],
    override_env: Mapping[str, str],
    fanin_env: Mapping[str, str],
    base_label: str,
    override_label: str,
    fanin_label: str,
) -> RuntimeSetting:
    operator_raw = _raw(base_env, key)
    override_raw = _raw(override_env, key)
    generated_raw = _raw(fanin_env, key)
    operator_value, operator_error = _positive_int(operator_raw)
    override_value, override_error = _positive_int(override_raw)
    generated_value, generated_error = _positive_int(generated_raw)
    warnings: list[str] = []

    if override_error is not None:
        warnings.append(
            f"{key} in {override_label} is invalid ({override_raw!r}: "
            f"{override_error}); ignored"
        )
    if operator_error is not None:
        warnings.append(
            f"{key} in {base_label} is invalid ({operator_raw!r}: "
            f"{operator_error}); ignored"
        )
    if generated_error is not None:
        warnings.append(
            f"{key} in {fanin_label} is invalid ({generated_raw!r}: "
            f"{generated_error}); using the next safe source"
        )
    if operator_raw is not None:
        warnings.append(
            f"{key} is present in {base_label}; fan-in buffer tuning belongs in "
            f"{fanin_label} or the audio runtime lab override artifact"
        )
    if operator_raw is not None and generated_raw is not None:
        warnings.append(
            f"{key} is set in both {base_label} and {fanin_label}; "
            f"{fanin_label} is the reconciler-owned home"
        )
    if override_raw is not None and (operator_raw is not None or generated_raw is not None):
        warnings.append(
            f"{key} lab override in {override_label} is active; it intentionally "
            "wins over env/default values"
        )
    if override_value is not None:
        return RuntimeSetting(
            key=key,
            value=override_value,
            source_kind="lab_override",
            source=override_label,
            unit="frames",
            override_value=override_raw,
            operator_value=operator_raw,
            generated_value=generated_raw,
            warnings=tuple(warnings),
        )
    if generated_value is not None:
        return RuntimeSetting(
            key=key,
            value=generated_value,
            source_kind="generated_env",
            source=fanin_label,
            unit="frames",
            operator_value=operator_raw,
            generated_value=generated_raw,
            warnings=tuple(warnings),
        )
    if operator_value is not None:
        return RuntimeSetting(
            key=key,
            value=operator_value,
            source_kind="operator_env",
            source=base_label,
            unit="frames",
            operator_value=operator_raw,
            generated_value=generated_raw,
            warnings=tuple(warnings),
        )
    return RuntimeSetting(
        key=key,
        value=default,
        source_kind="packaged_default",
        source="packaged fan-in default",
        unit="frames",
        operator_value=operator_raw,
        generated_value=generated_raw,
        warnings=tuple(warnings),
    )


def _resolve_coupling(
    *,
    base_env: Mapping[str, str],
    override_env: Mapping[str, str],
    fanin_env: Mapping[str, str],
    base_label: str,
    override_label: str,
    fanin_label: str,
) -> RuntimeSetting:
    base_raw = _raw(base_env, COUPLING_ENV_VAR)
    unsupported_override_raw = _raw(override_env, COUPLING_ENV_VAR)
    fanin_raw = _raw(fanin_env, COUPLING_ENV_VAR)
    raw = fanin_raw if fanin_raw is not None else base_raw
    coupling = resolve_coupling(raw)
    warnings: list[str] = []
    if unsupported_override_raw is not None:
        warnings.append(
            f"{COUPLING_ENV_VAR} in {override_label} is ignored; fan-in "
            "coupling transitions are owned by jasper-fanin-coupling-reconcile"
        )
    if base_raw is not None:
        warnings.append(
            f"{COUPLING_ENV_VAR} is present in {base_label}; "
            f"{fanin_label} is the reconciler-owned home"
        )
    if base_raw is not None and fanin_raw is not None:
        warnings.append(
            f"{COUPLING_ENV_VAR} is set in both {base_label} and {fanin_label}; "
            f"{fanin_label} wins"
        )
    if raw is not None and raw.strip().lower() not in _VALID_COUPLINGS:
        warnings.append(
            f"{COUPLING_ENV_VAR}={raw!r} is not recognized; resolved to "
            f"{COUPLING_LOOPBACK}"
        )
    return RuntimeSetting(
        key=COUPLING_ENV_VAR,
        value=coupling,
        source_kind=(
            "generated_env" if fanin_raw is not None
            else "packaged_default"
        ),
        source=(
            fanin_label if fanin_raw is not None
            else "packaged fan-in default"
        ),
        generated_value=fanin_raw,
        operator_value=base_raw,
        warnings=tuple(warnings),
    )
