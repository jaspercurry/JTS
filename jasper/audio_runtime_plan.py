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

import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, TypedDict, cast

from jasper.audio_hardware.dac import by_id as dac_profile_by_id
from jasper.audio_hardware.dac import latency_floor_for
from jasper.audio_runtime_overrides import (
    DEFAULT_AUDIO_RUNTIME_OVERRIDES_PATH,
    load_runtime_overrides,
    runtime_overrides_path,
)
from jasper.camilla_config_contract import (
    ACTIVE_OUTPUTD_PLAYBACK_DEVICE,
    DEFAULT_CAPTURE_DEVICE,
    DEFAULT_CAPTURE_FORMAT,
    DEFAULT_CHUNKSIZE,
    DEFAULT_PLAYBACK_DEVICE,
    DEFAULT_PLAYBACK_FORMAT,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_TARGET_LEVEL,
    outputd_capture_device_for_playback,
    read_camilla_devices_config,
)
from jasper.env_load import read_env_file_state
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_LOOPBACK,
    COUPLING_SHM_RING,
    OUTPUTD_CONTENT_BRIDGE_SHM_RING,
    RING_CAMILLA_CHUNKSIZE,
    RING_CAMILLA_ENABLE_RATE_ADJUST,
    RING_CAMILLA_QUEUELIMIT,
    RING_CAMILLA_TARGET_LEVEL,
    VALID_COUPLINGS,
    capture_kwargs_for_coupling,
    coupling_capture_kwargs_from_env,
    member_kwargs_are_pipe_sink,
    resolve_coupling,
    resolve_outputd_content_bridge,
    ring_pair_is_coherent,
)


DEFAULT_BASE_ENV_PATH = "/etc/jasper/jasper.env"
DEFAULT_OUTPUTD_ENV_PATH = "/var/lib/jasper/outputd.env"
DEFAULT_FANIN_ENV_PATH = "/var/lib/jasper/fanin.env"
DEFAULT_GROUPING_ENV_PATH = "/var/lib/jasper/grouping.env"
DEFAULT_CAMILLA_STATEFILE_PATH = "/var/lib/camilladsp/outputd-statefile.yml"
DEFAULT_CAMILLA2_STATEFILE_PATH = "/var/lib/camilladsp/crossover-statefile.yml"

OUTPUTD_PERIOD_KEY = "JASPER_OUTPUTD_PERIOD_FRAMES"
OUTPUTD_CONTENT_BUFFER_KEY = "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES"
OUTPUTD_DAC_BUFFER_KEY = "JASPER_OUTPUTD_DAC_BUFFER_FRAMES"
OUTPUTD_MIN_BUFFER_PERIOD_MULTIPLIER = 2
DEFAULT_OUTPUTD_PERIOD_FRAMES = 1024
DEFAULT_OUTPUTD_CONTENT_BUFFER_FRAMES = 4096
DEFAULT_OUTPUTD_DAC_BUFFER_FRAMES = 3072
OUTPUTD_CONTENT_BRIDGE_KEY = "JASPER_OUTPUTD_CONTENT_BRIDGE"
OUTPUTD_CONTENT_BRIDGE_DIRECT = "direct"
MAX_LOW_LATENCY_CORRECTION_GROUP_DELAY_FRAMES = 512
FANIN_INPUT_BUFFER_KEY = "JASPER_FANIN_INPUT_BUFFER_FRAMES"
FANIN_OUTPUT_BUFFER_KEY = "JASPER_FANIN_OUTPUT_BUFFER_FRAMES"
FANIN_ADAPTIVE_SHRUNK_FRAMES_ENV = "JASPER_FANIN_ADAPTIVE_SHRUNK_FRAMES"
DEFAULT_FANIN_INPUT_BUFFER_FRAMES = 4096
DEFAULT_FANIN_OUTPUT_BUFFER_FRAMES = 1024
MIN_FANIN_OUTPUT_BUFFER_FRAMES = 1024
FANIN_INPUT_RESAMPLER_KEY = "JASPER_FANIN_INPUT_RESAMPLER"
FANIN_INPUT_RESAMPLER_LANE_KEY = "JASPER_FANIN_INPUT_RESAMPLER_LANE"
FANIN_INPUT_RESAMPLER_TARGET_KEY = "JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES"
FANIN_INPUT_RESAMPLER_MAX_ADJUST_KEY = "JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM"
FANIN_INPUT_RESAMPLER_CUSHION_KEY = (
    "JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES"
)
FANIN_INPUT_RESAMPLER_RING_KEY = "JASPER_FANIN_INPUT_RESAMPLER_RING_FRAMES"
FANIN_USB_DIRECT_PERIOD_KEY = "JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES"
FANIN_USB_DIRECT_DEVICE = "hw:UAC2Gadget"
DEFAULT_FANIN_USB_DIRECT_PERIOD_FRAMES = 256
MIN_FANIN_USB_DIRECT_PERIOD_FRAMES = 32
MAX_FANIN_USB_DIRECT_PERIOD_FRAMES = 1024
FANIN_USB_DIRECT_MIN_BUFFER_FRAMES = 768
FANIN_USB_DIRECT_MIN_BUFFER_PERIODS = 3
DEFAULT_USB_LOW_LATENCY_RESAMPLER_TARGET_FRAMES = 512
DEFAULT_USB_LOW_LATENCY_RESAMPLER_MAX_ADJUST_PPM = 500
DEFAULT_USB_LOW_LATENCY_RESAMPLER_CUSHION_FRAMES = 1536
DEFAULT_USB_LOW_LATENCY_RESAMPLER_RING_FRAMES = 4096
DEFAULT_USB_LOW_LATENCY_OUTPUTD_CONTENT_BUFFER_FRAMES = 1536
AUDIO_ROUTE_PROFILE_KEY = "JASPER_AUDIO_ROUTE_PROFILE"
ROUTE_CORRECTED_48K = "corrected_48k"
ROUTE_USB_LOW_LATENCY_48K = "usb_low_latency_48k"
ROUTE_BITPERFECT_DECLARED = "bitperfect_passthrough_declared"
ROUTE_LATENCY_PROFILE = "route_latency"
USB_LOW_LATENCY_SOURCE_ID = "usbsink"
# Route-latency certification budget at the electrical tap->:9891 plane.
# Measured basis: the 2026-07-11 promotion certification on jts.local (artifact
# 20260711T234400.457205Z__route_latency__apple_usb_c_dongle__route_latency__pass.json,
# build d5abf5ad, route_hash 3bca2569c864ad1a) measured p50 36.35 / p95 37.93 /
# p99 38.29 / max 38.48 ms over 1094 matched impulses (100% match, 32.6 min,
# zero outliers) at the 576-frame churn-safe floor, electrical :9891 plane,
# flow-gated streaming detector. See docs/HANDOFF-usb-latency-measurement.md §1.
# 40.0 sits 2.1 ms over the measured p95 (37.93) and 1.5 ms over the observed
# max (38.48); 42.0 is the tail budget, 2 ms above the p95 gate and ~3.7 ms over
# the measured p99 (38.29) — so any >=2 ms regression trips the gate. The gate
# is a cert-time tripwire only: no runtime consumer reads these constants. Flap
# protocol: a marginal fail gets ONE clean re-run (steady-state, flow-gated)
# before it is treated as a regression; loosening these numbers requires new
# measured evidence.
# NOTE: these constants ride AudioRouteProfile.to_dict() into
# route_config_hash_for_plan, so tightening them changes the route_config_hash
# for any usb_low_latency_48k box. The 2026-07-11 artifact (certified against
# the prior 48/60 budget) therefore reads config_mismatch until ONE fresh
# certification run re-certifies against 40/42 — its measured numbers clear the
# new gate with margin.
USB_LOW_LATENCY_P95_BUDGET_MS = 40.0
USB_LOW_LATENCY_P99_BUDGET_MS = 42.0
ROUTE_CONFIG_HASH_SCHEMA_VERSION = 4
UAC2_LOW_LATENCY_EXPECTED_ATTRS = {
    "c_sync": "async",
    "req_number": "2",
    "c_hs_bint": "1",
}

OUTPUTD_LATENCY_KEYS = (
    "JASPER_CAMILLA_CHUNKSIZE",
    "JASPER_CAMILLA_TARGET_LEVEL",
    OUTPUTD_PERIOD_KEY,
    OUTPUTD_CONTENT_BUFFER_KEY,
    OUTPUTD_DAC_BUFFER_KEY,
)
AUDIO_RUNTIME_OVERRIDE_KEYS = frozenset(
    OUTPUTD_LATENCY_KEYS
    + (
        FANIN_INPUT_BUFFER_KEY,
        FANIN_OUTPUT_BUFFER_KEY,
    )
)
BASE_ENV_PROCESS_FALLBACK_KEYS = frozenset(
    AUDIO_RUNTIME_OVERRIDE_KEYS
    | {
        AUDIO_ROUTE_PROFILE_KEY,
        COUPLING_ENV_VAR,
        FANIN_USB_DIRECT_PERIOD_KEY,
    }
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

# Reuse fanin_coupling's SSOT so the plan recognizes every coupling the
# resolver does — including the Ring A ``shm_ring`` product transport. The plan
# does not keep an independent coupling set (that would drift from the resolver
# and false-warn on a new transport).
_VALID_COUPLINGS = VALID_COUPLINGS
_VALID_AUDIO_ROUTE_PROFILES = {
    ROUTE_CORRECTED_48K,
    ROUTE_USB_LOW_LATENCY_48K,
    ROUTE_BITPERFECT_DECLARED,
}

# The route modes that mean "grouping is enabled" (leader/follower = enabled +
# valid + role; invalid_grouping = enabled + config error). ``shm_ring`` is
# solo-stereo-only until P8's ring-v2 (N-channel + bonded round-trip), so arming
# it on ANY of these would split camilla#1's graph across topologies (the bonded
# leader-pipe / round-trip lanes assume the loopback/aloop content path, not the
# SHM ring) and silence the leader's own local output. This is the SYMMETRIC half
# of jasper.multiroom.reconcile's "ring-armed box cannot bond" gate (which blocks
# the OTHER direction — forming a bond while already ring-armed); together they
# make ring ⟂ grouping a fail-closed invariant from both entry points. ``solo`` /
# ``unknown`` are NOT blocked: solo = grouping off, unknown = indeterminate (a
# transient grouping-config read failure must not refuse a legitimate solo arm).
_GROUPING_ENABLED_ROUTE_MODES = frozenset(
    {"active_leader", "active_follower", "invalid_grouping"}
)
_GROUPED_SHM_RING_REASON = "fanin_shm_ring_coupling_unsupported_while_grouped"
_GROUPED_SHM_RING_DETAIL = (
    "JASPER_FANIN_CAMILLA_COUPLING=shm_ring is not supported while this box has "
    "multiroom grouping enabled; the SHM ring is solo-stereo-only until ring v2 "
    "(P8), and arming it on a bonded box would strand the leader's local output "
    "(outputd reads Ring B while camilla#1 still bakes the aloop/loopback grouped "
    "program). Disarm the ring (jasper-fanin-coupling-reconcile loopback) or "
    "ungroup this speaker; keeping the coupling on loopback."
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
    # Ring (shm_ring) coupling names its CamillaDSP capture/playback devices via
    # ALSA ioplug devices (jts_ring_capture / jts_ring_playback), so BOTH device
    # and format ride the coupling kwargs.
    capture_device: str
    capture_format: str
    playback_device: str
    playback_format: str
    chunksize: int
    target_level: int
    queuelimit: int


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
    that consumes it. Today one experimental consumer uses it:
    ``JASPER_FANIN_ADAPTIVE_BUFFER`` shrinks fan-in's output buffer. A future
    low-latency source route should change this support matrix/consumer set
    here rather than re-implementing the same source-exclusivity check in each
    caller.
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
class OutputEndpointEvidence:
    """Loaded CamillaDSP endpoint evidence plus any unreadable inputs."""

    devices: Mapping[str, Any] | None
    errors: tuple[str, ...] = ()
    endpoint_recognized: bool = True


@dataclass(frozen=True)
class AudioRouteProfile:
    """Resolved processing-route contract for latency claims."""

    route_id: str
    source_id: str
    fixed_sample_rate: int
    low_latency_claim: bool
    fanin_usb_direct_required: bool
    fanin_input_resampler_required: bool
    camilla_required: bool
    outputd_final_reference_required: bool
    bitperfect: bool = False
    active: bool = True
    aec_reference_mode: str = "outputd_final_electrical"
    p95_budget_ms: float | None = None
    p99_budget_ms: float | None = None
    evidence_profile: str | None = None
    blocking_reason: str = ""
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "route_id": self.route_id,
            "source_id": self.source_id,
            "fixed_sample_rate": self.fixed_sample_rate,
            "low_latency_claim": self.low_latency_claim,
            "fanin_usb_direct_required": self.fanin_usb_direct_required,
            "fanin_input_resampler_required": self.fanin_input_resampler_required,
            "camilla_required": self.camilla_required,
            "outputd_final_reference_required": (
                self.outputd_final_reference_required
            ),
            "bitperfect": self.bitperfect,
            "active": self.active,
            "aec_reference_mode": self.aec_reference_mode,
        }
        if self.p95_budget_ms is not None:
            out["p95_budget_ms"] = self.p95_budget_ms
        if self.p99_budget_ms is not None:
            out["p99_budget_ms"] = self.p99_budget_ms
        if self.evidence_profile:
            out["evidence_profile"] = self.evidence_profile
        if self.blocking_reason:
            out["blocking_reason"] = self.blocking_reason
        if self.warnings:
            out["warnings"] = list(self.warnings)
        return out


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
    route_profile: AudioRouteProfile
    route_config_hash: str
    camilla_config_hash: str
    correction_latency_eligibility: CorrectionLatencyEligibility
    route_policy_errors: tuple[str, ...] = ()
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
        if (
            self.route_profile.low_latency_claim
            and not self.correction_latency_eligibility.eligible
        ):
            out.append(
                "low-latency route is blocked by correction latency: "
                f"{self.correction_latency_eligibility.blocking_reason}"
            )
        if self.route_profile.blocking_reason:
            out.append(self.route_profile.blocking_reason)
        out.extend(self.route_policy_errors)
        return tuple(out)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "profile_label": self.profile_label,
            "route_mode": self.route_mode,
            "settings": [setting.to_dict() for setting in self.settings],
            "coupling_support": self.coupling_support.to_dict(),
            "transport_topology": self.transport_topology.to_dict(),
            "route_profile": self.route_profile.to_dict(),
            "route_config_hash": self.route_config_hash,
            "camilla_config_hash": self.camilla_config_hash,
            "route_latency_identity": self.route_latency_identity(),
            "correction_latency_eligibility": (
                self.correction_latency_eligibility.to_dict()
            ),
            "route_policy_errors": list(self.route_policy_errors),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }

    def route_latency_identity(self) -> dict[str, Any]:
        """Expected identity fields for a route-latency validation artifact."""

        return route_latency_identity_for_plan(
            route=self.route_profile,
            settings=self.settings,
            route_config_hash=self.route_config_hash,
            camilla_config_hash=self.camilla_config_hash,
            dac_profile_id=None if self.profile_id == "unknown" else self.profile_id,
        )


def minimum_outputd_buffer_frames(period_frames: int) -> int:
    """Minimum outputd ALSA buffer for one period, matching Rust validation."""

    return period_frames * OUTPUTD_MIN_BUFFER_PERIOD_MULTIPLIER


def outputd_buffer_pair_error(
    *,
    buffer_name: str,
    buffer_frames: int,
    period_name: str,
    period_frames: int,
) -> str | None:
    """Return Rust-shaped detail when an outputd buffer/period pair is invalid."""

    min_buffer_frames = minimum_outputd_buffer_frames(period_frames)
    if buffer_frames >= min_buffer_frames:
        return None
    return (
        f"{buffer_name}={buffer_frames} must be >= "
        f"{OUTPUTD_MIN_BUFFER_PERIOD_MULTIPLIER} x {period_name}={period_frames} "
        "(minimum ALSA jitter margin)"
    )


def outputd_content_buffer_pair_error(
    *,
    period_frames: int,
    content_buffer_frames: int,
) -> str | None:
    """Return the content-buffer invariant error that maps to outputd exit 78."""

    return outputd_buffer_pair_error(
        buffer_name=OUTPUTD_CONTENT_BUFFER_KEY,
        buffer_frames=content_buffer_frames,
        period_name=OUTPUTD_PERIOD_KEY,
        period_frames=period_frames,
    )


def outputd_dac_buffer_pair_error(
    *,
    period_frames: int,
    dac_buffer_frames: int,
) -> str | None:
    """Return the DAC-buffer invariant error that maps to outputd exit 78."""

    return outputd_buffer_pair_error(
        buffer_name=OUTPUTD_DAC_BUFFER_KEY,
        buffer_frames=dac_buffer_frames,
        period_name=OUTPUTD_PERIOD_KEY,
        period_frames=period_frames,
    )


def outputd_env_buffer_pair_error(
    *,
    base_env: Mapping[str, str] | None = None,
    outputd_env: Mapping[str, str] | None = None,
) -> str | None:
    """Validate effective outputd buffer/period pairs for env-file writers.

    Precedence mirrors the service contract: packaged defaults, then
    ``/etc/jasper/jasper.env``, then the reconciler-owned ``outputd.env``.
    The check order mirrors Rust's outputd config validator so logs name the
    same first failing pair the daemon would reject with EX_CONFIG.
    """

    values = [dict(base_env or {}), dict(outputd_env or {})]
    period_frames, period_error = _effective_outputd_positive_int(
        OUTPUTD_PERIOD_KEY,
        default=DEFAULT_OUTPUTD_PERIOD_FRAMES,
        layers=values,
    )
    if period_error is not None:
        return period_error
    content_buffer_frames, content_error = _effective_outputd_positive_int(
        OUTPUTD_CONTENT_BUFFER_KEY,
        default=DEFAULT_OUTPUTD_CONTENT_BUFFER_FRAMES,
        layers=values,
    )
    if content_error is not None:
        return content_error
    detail = outputd_content_buffer_pair_error(
        period_frames=period_frames,
        content_buffer_frames=content_buffer_frames,
    )
    if detail is not None:
        return detail
    dac_buffer_frames, dac_error = _effective_outputd_positive_int(
        OUTPUTD_DAC_BUFFER_KEY,
        default=DEFAULT_OUTPUTD_DAC_BUFFER_FRAMES,
        layers=values,
    )
    if dac_error is not None:
        return dac_error
    return outputd_dac_buffer_pair_error(
        period_frames=period_frames,
        dac_buffer_frames=dac_buffer_frames,
    )


def coupling_supported_for_route(coupling: str, route_mode: RouteMode) -> CouplingSupport:
    """Return whether ``coupling`` is supported for ``route_mode``.

    One blocked combination, because a non-loopback coupling assumes a
    solo-speaker content path that a grouped camilla#1 graph does not have:

    - ``shm_ring`` + any grouping-enabled mode (leader/follower/invalid) — the ring
      is solo-stereo-only until ring v2 (P8); arming it on a bonded box would
      strand the leader's local output. This is the symmetric half of the
      multiroom reconciler's "ring-armed box cannot bond" gate.

    Keeping this in a route-policy function makes grouped coupling support a
    deliberate support-matrix change instead of another scattered conditional.
    ``solo`` / ``unknown`` never block: solo = grouping off, unknown = a transient
    indeterminate read that must not refuse a legitimate solo arm.
    """

    normalized = resolve_coupling(coupling)
    mode = route_mode if route_mode in _VALID_ROUTE_MODES else "unknown"
    if normalized == COUPLING_SHM_RING and mode in _GROUPING_ENABLED_ROUTE_MODES:
        return CouplingSupport(
            coupling=normalized,
            route_mode=mode,  # type: ignore[arg-type]
            supported=False,
            reason=_GROUPED_SHM_RING_REASON,
            detail=_GROUPED_SHM_RING_DETAIL,
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

    Only the exact literal ``enabled`` (case-insensitive, stripped) turns the
    experiment on; unset, ``disabled``, ``1``, and ``true`` all stay off.
    """

    if env is None:
        env = os.environ
    return LowLatencyFeatureFlags(
        adaptive_buffer=_enabled_literal(env.get("JASPER_FANIN_ADAPTIVE_BUFFER")),
    )


def _source_id(source: Any) -> str:
    value = getattr(source, "value", source)
    return str(value).strip()


def _enabled_literal(raw: str | None) -> bool:
    return (raw or "").strip().lower() == "enabled"


def resolve_audio_route_profile(
    env: Mapping[str, str] | None = None,
) -> AudioRouteProfile:
    """Resolve the audio processing route.

    Unknown route ids fail safe to ``corrected_48k`` and carry a warning. The
    route contract is intentionally separate from source selection: it says what
    a route is allowed to claim, while mux/fan-in still decide what source is
    currently audible.
    """

    values = dict(os.environ if env is None else env)
    raw = str(values.get(AUDIO_ROUTE_PROFILE_KEY, "")).strip().lower()
    route_id = raw or ROUTE_CORRECTED_48K
    warnings: tuple[str, ...] = ()
    if route_id not in _VALID_AUDIO_ROUTE_PROFILES:
        warnings = (
            f"invalid {AUDIO_ROUTE_PROFILE_KEY}={raw!r}; using {ROUTE_CORRECTED_48K}",
        )
        route_id = ROUTE_CORRECTED_48K

    if route_id == ROUTE_USB_LOW_LATENCY_48K:
        return AudioRouteProfile(
            route_id=route_id,
            source_id=USB_LOW_LATENCY_SOURCE_ID,
            fixed_sample_rate=DEFAULT_SAMPLE_RATE,
            low_latency_claim=True,
            fanin_usb_direct_required=True,
            fanin_input_resampler_required=True,
            camilla_required=True,
            outputd_final_reference_required=True,
            p95_budget_ms=USB_LOW_LATENCY_P95_BUDGET_MS,
            p99_budget_ms=USB_LOW_LATENCY_P99_BUDGET_MS,
            evidence_profile=ROUTE_LATENCY_PROFILE,
            warnings=warnings,
        )

    if route_id == ROUTE_BITPERFECT_DECLARED:
        return AudioRouteProfile(
            route_id=route_id,
            source_id=USB_LOW_LATENCY_SOURCE_ID,
            fixed_sample_rate=0,
            low_latency_claim=False,
            fanin_usb_direct_required=False,
            fanin_input_resampler_required=False,
            camilla_required=False,
            outputd_final_reference_required=True,
            bitperfect=True,
            active=False,
            aec_reference_mode="degraded_until_final_reference_proven",
            blocking_reason=(
                "bit-perfect passthrough is declared but inactive; it must "
                "prove passive/full-range safety and final-reference truth "
                "before activation"
            ),
            warnings=warnings,
        )

    return AudioRouteProfile(
        route_id=ROUTE_CORRECTED_48K,
        source_id="all",
        fixed_sample_rate=DEFAULT_SAMPLE_RATE,
        low_latency_claim=False,
        fanin_usb_direct_required=False,
        fanin_input_resampler_required=False,
        camilla_required=True,
        outputd_final_reference_required=True,
        warnings=warnings,
    )


def route_owned_env_actions(
    route: AudioRouteProfile | str,
) -> tuple[RuntimeEnvAction, ...]:
    """Return generated-env actions implied by an audio route profile."""

    profile = (
        resolve_audio_route_profile({AUDIO_ROUTE_PROFILE_KEY: route})
        if isinstance(route, str)
        else route
    )
    if profile.route_id != ROUTE_USB_LOW_LATENCY_48K:
        return (
            RuntimeEnvAction("unset", FANIN_INPUT_RESAMPLER_KEY),
            RuntimeEnvAction("unset", FANIN_INPUT_RESAMPLER_LANE_KEY),
            RuntimeEnvAction("unset", FANIN_INPUT_RESAMPLER_TARGET_KEY),
            RuntimeEnvAction("unset", FANIN_INPUT_RESAMPLER_MAX_ADJUST_KEY),
            RuntimeEnvAction("unset", FANIN_INPUT_RESAMPLER_CUSHION_KEY),
            RuntimeEnvAction("unset", FANIN_INPUT_RESAMPLER_RING_KEY),
        )

    return (
        RuntimeEnvAction("set", FANIN_INPUT_RESAMPLER_KEY, "enabled"),
        RuntimeEnvAction("set", FANIN_INPUT_RESAMPLER_LANE_KEY, USB_LOW_LATENCY_SOURCE_ID),
        RuntimeEnvAction(
            "set",
            FANIN_INPUT_RESAMPLER_TARGET_KEY,
            str(DEFAULT_USB_LOW_LATENCY_RESAMPLER_TARGET_FRAMES),
        ),
        RuntimeEnvAction(
            "set",
            FANIN_INPUT_RESAMPLER_MAX_ADJUST_KEY,
            str(DEFAULT_USB_LOW_LATENCY_RESAMPLER_MAX_ADJUST_PPM),
        ),
        RuntimeEnvAction(
            "set",
            FANIN_INPUT_RESAMPLER_CUSHION_KEY,
            str(DEFAULT_USB_LOW_LATENCY_RESAMPLER_CUSHION_FRAMES),
        ),
        RuntimeEnvAction(
            "set",
            FANIN_INPUT_RESAMPLER_RING_KEY,
            str(DEFAULT_USB_LOW_LATENCY_RESAMPLER_RING_FRAMES),
        ),
    )


def camilla_config_hash_for_path(path: str | None) -> str:
    """Return a stable short content hash for the active Camilla config."""

    if not path:
        return ""
    try:
        body = Path(path).read_bytes()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    return hashlib.sha256(body).hexdigest()[:16]


def _route_action_values(route: AudioRouteProfile) -> dict[str, str]:
    return {
        action.key: action.value
        for action in route_owned_env_actions(route)
        if action.action == "set"
    }


def _int_like(value: str | int) -> int | str:
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def fanin_resampler_config_for_route(route: AudioRouteProfile) -> dict[str, Any]:
    """Route-owned fan-in resampler config expected for latency evidence."""

    values = _route_action_values(route)
    if values.get(FANIN_INPUT_RESAMPLER_KEY) != "enabled":
        return {}
    return {
        "enabled": True,
        "lane": values.get(FANIN_INPUT_RESAMPLER_LANE_KEY, ""),
        "target_frames": _int_like(
            values.get(FANIN_INPUT_RESAMPLER_TARGET_KEY, ""),
        ),
        "max_adjust_ppm": _int_like(
            values.get(FANIN_INPUT_RESAMPLER_MAX_ADJUST_KEY, ""),
        ),
        "warmup_cushion_frames": _int_like(
            values.get(FANIN_INPUT_RESAMPLER_CUSHION_KEY, ""),
        ),
        "ring_frames": _int_like(values.get(FANIN_INPUT_RESAMPLER_RING_KEY, "")),
    }


def _fanin_direct_min_buffer_frames(period_frames: int) -> int:
    """Return fan-in's smallest valid deep, period-aligned direct buffer."""

    floor = max(
        period_frames * FANIN_USB_DIRECT_MIN_BUFFER_PERIODS,
        FANIN_USB_DIRECT_MIN_BUFFER_FRAMES,
    )
    return ((floor + period_frames - 1) // period_frames) * period_frames


def fanin_direct_config_for_route(
    route: AudioRouteProfile,
    settings: tuple[RuntimeSetting, ...],
) -> dict[str, Any]:
    """Planned USB direct-capture contract expected in fan-in STATUS."""

    if not route.fanin_usb_direct_required:
        return {}
    period_frames = int(
        next(
            setting.value
            for setting in settings
            if setting.key == FANIN_USB_DIRECT_PERIOD_KEY
        )
    )
    return {
        "lane": USB_LOW_LATENCY_SOURCE_ID,
        "source": "direct",
        "device": FANIN_USB_DIRECT_DEVICE,
        "period_frames": period_frames,
        "min_buffer_frames": _fanin_direct_min_buffer_frames(period_frames),
        "buffer_period_aligned": True,
    }


def outputd_config_for_settings(
    settings: tuple[RuntimeSetting, ...],
) -> dict[str, Any]:
    """Output/Camilla buffering knobs that are part of latency identity."""

    keys = set(OUTPUTD_LATENCY_KEYS)
    return {
        setting.key: setting.value
        for setting in settings
        if setting.key in keys
    }


def route_latency_identity_for_plan(
    *,
    route: AudioRouteProfile,
    settings: tuple[RuntimeSetting, ...],
    route_config_hash: str,
    camilla_config_hash: str,
    dac_profile_id: str | None = None,
) -> dict[str, Any]:
    """Expected validation identity for the effective low-latency route."""

    return {
        "route_id": route.route_id,
        "source_id": route.source_id,
        "dac_profile_id": dac_profile_id or "",
        "route_config_hash": route_config_hash,
        "camilla_config_hash": camilla_config_hash,
        "fanin_direct_config": fanin_direct_config_for_route(route, settings),
        "fanin_resampler_config": fanin_resampler_config_for_route(route),
        "outputd_config": outputd_config_for_settings(settings),
        "uac2_gadget_attrs": (
            dict(UAC2_LOW_LATENCY_EXPECTED_ATTRS)
            if route.route_id == ROUTE_USB_LOW_LATENCY_48K
            else {}
        ),
    }


def route_config_hash_for_plan(
    *,
    route: AudioRouteProfile,
    settings: tuple[RuntimeSetting, ...],
    coupling: str,
    correction_latency: CorrectionLatencyEligibility,
    camilla_config_hash: str = "",
) -> str:
    """Stable short hash for matching latency artifacts to this plan."""

    payload = {
        "schema_version": ROUTE_CONFIG_HASH_SCHEMA_VERSION,
        "route": route.to_dict(),
        "route_env_actions": [
            action.to_dict()
            for action in route_owned_env_actions(route)
        ],
        "settings": [setting.to_dict() for setting in settings],
        "coupling": coupling,
        "correction_latency": correction_latency.to_dict(),
        "camilla_config_hash": camilla_config_hash,
        "uac2_gadget_attrs": (
            dict(UAC2_LOW_LATENCY_EXPECTED_ATTRS)
            if route.route_id == ROUTE_USB_LOW_LATENCY_48K
            else {}
        ),
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _route_policy_errors(
    *,
    route: AudioRouteProfile,
    coupling: str,
    outputd_env: Mapping[str, str],
    camilla_devices: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    errors = list(
        transport_coherence_errors(
            coupling=coupling,
            outputd_env=outputd_env,
            camilla_devices=camilla_devices,
        )
    )
    if route.route_id != ROUTE_USB_LOW_LATENCY_48K:
        return tuple(errors)

    normalized_coupling = resolve_coupling(coupling)
    # RAW bridge value (lowercased) — NOT resolve_outputd_content_bridge, which
    # fail-safes an unknown bridge (e.g. the deferred lab `rate_match`) to
    # `direct` and would hide it here. The route policy must reject `rate_match`
    # explicitly, so it compares the operator's literal value.
    raw_bridge = str(
        outputd_env.get(OUTPUTD_CONTENT_BRIDGE_KEY, OUTPUTD_CONTENT_BRIDGE_DIRECT)
        or OUTPUTD_CONTENT_BRIDGE_DIRECT
    ).strip().lower()

    # usb_low_latency_48k is certifiable on EITHER of the two COHERENT transports:
    # loopback + direct (legacy/fail-safe fallback), OR the shm_ring pair (Ring A
    # + Ring B, the shipped default on eligible stereo boxes). The ring pair was
    # measured to reduce route latency (the
    # ring-proto train), so the artifact binder must accept it — the earlier
    # blanket "requires loopback + direct" would turn a ring-armed box's shipped
    # low-latency claim permanently red (gap 8). The outputd rate_match content
    # bridge stays rejected (a deferred lab transport that failed the 2026-07-02
    # USB tuning).
    if normalized_coupling == COUPLING_SHM_RING and ring_pair_is_coherent(
        normalized_coupling, raw_bridge
    ):
        return tuple(errors)

    if normalized_coupling != COUPLING_LOOPBACK:
        errors.append(
            f"{ROUTE_USB_LOW_LATENCY_48K} requires {COUPLING_ENV_VAR}=loopback or a "
            f"coherent shm_ring pair; {normalized_coupling} is not coherent for "
            "the production low-latency claim"
        )
    if raw_bridge != OUTPUTD_CONTENT_BRIDGE_DIRECT:
        errors.append(
            f"{ROUTE_USB_LOW_LATENCY_48K} requires "
            f"{OUTPUTD_CONTENT_BRIDGE_KEY}={OUTPUTD_CONTENT_BRIDGE_DIRECT} (or the "
            f"coherent shm_ring pair); {raw_bridge} without a matching "
            f"{COUPLING_ENV_VAR}=shm_ring is a partial flip"
        )
    return tuple(errors)


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
    base_values = dict(base.values)
    env_read_warnings: list[str] = []
    if base.status == "unreadable":
        base_values.update(
            {
                key: value
                for key, value in os.environ.items()
                if key in BASE_ENV_PROCESS_FALLBACK_KEYS
            }
        )
    for label, state in (
        ("base", base),
        ("outputd", outputd),
        ("fanin", fanin),
    ):
        if state.status == "unreadable":
            detail = f": {state.error}" if state.error else ""
            env_read_warnings.append(
                f"unreadable audio runtime {label} env file {state.path}{detail}; "
                "runtime plan may be using stale or partial settings"
            )
    resolved_overrides_path = overrides_path or runtime_overrides_path()
    overrides = load_runtime_overrides(
        resolved_overrides_path,
        allowed_keys=AUDIO_RUNTIME_OVERRIDE_KEYS,
    )
    profile_id = ""
    try:
        from jasper.output_hardware import load_state

        hardware_state = load_state(output_hardware_state_path)
        if hardware_state is not None:
            profile_id = hardware_state.profile_id
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
        base_env=base_values,
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
        plan_warnings=tuple(env_read_warnings) + overrides.warnings,
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
    route_profile = resolve_audio_route_profile(base_values)

    camilla_chunksize_setting = _resolve_profile_floor_int(
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
    )
    coupling_setting = _resolve_coupling(
        base_env=base_values,
        override_env=override_values,
        fanin_env=fanin_values,
        base_label=base_env_label,
        override_label=override_label,
        fanin_label=fanin_env_label,
    )
    camilla_chunksize_setting = _effective_camilla_chunksize_setting(
        chunksize_setting=camilla_chunksize_setting,
        coupling=str(coupling_setting.value),
    )
    camilla_target_setting = _resolve_profile_floor_int(
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
    )
    camilla_target_setting = _effective_camilla_target_setting(
        target_setting=camilla_target_setting,
        coupling=str(coupling_setting.value),
    )
    outputd_period_setting = _resolve_profile_floor_int(
        key=OUTPUTD_PERIOD_KEY,
        default=DEFAULT_OUTPUTD_PERIOD_FRAMES,
        floor_value=getattr(floor, "outputd_period_frames", None),
        base_env=base_values,
        override_env=override_values,
        generated_env=outputd_values,
        base_label=base_env_label,
        override_label=override_label,
        generated_label=outputd_env_label,
        profile_id=profile_id,
    )
    outputd_content_buffer_setting = _coherent_outputd_content_buffer_setting(
        period_setting=outputd_period_setting,
        content_buffer_setting=_resolve_outputd_content_buffer_int(
            route=route_profile,
            base_env=base_values,
            override_env=override_values,
            generated_env=outputd_values,
            base_label=base_env_label,
            override_label=override_label,
            generated_label=outputd_env_label,
        ),
    )
    settings = [
        camilla_chunksize_setting,
        camilla_target_setting,
        outputd_period_setting,
        outputd_content_buffer_setting,
        _resolve_profile_floor_int(
            key=OUTPUTD_DAC_BUFFER_KEY,
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
    if route_profile.fanin_usb_direct_required:
        settings.append(
            _resolve_fanin_int(
                key=FANIN_USB_DIRECT_PERIOD_KEY,
                default=DEFAULT_FANIN_USB_DIRECT_PERIOD_FRAMES,
                base_env=base_values,
                # This lever is read by fan-in from its environment; the lab
                # override artifact has no writer for it and must not create a
                # plan-only value that the daemon never receives.
                override_env={},
                fanin_env=fanin_values,
                base_label=base_env_label,
                override_label=override_label,
                fanin_label=fanin_env_label,
                operator_env_allowed=True,
                min_value=MIN_FANIN_USB_DIRECT_PERIOD_FRAMES,
                max_value=MAX_FANIN_USB_DIRECT_PERIOD_FRAMES,
            )
        )
    settings.append(coupling_setting)
    support = coupling_supported_for_route(str(coupling_setting.value), route_mode)
    camilla_devices = read_camilla_devices_config(correction_config_path)
    topology = transport_topology_for_coupling(
        str(coupling_setting.value),
        fanin_env=fanin_values,
        outputd_env=outputd_values,
        camilla_playback_device=(
            str(camilla_devices.get("playback_device") or "")
            if camilla_devices is not None
            else None
        ),
    )
    correction_latency = correction_latency_eligibility_for_config(
        correction_config_path
    )
    camilla_config_hash = camilla_config_hash_for_path(correction_config_path)
    route_hash = route_config_hash_for_plan(
        route=route_profile,
        settings=tuple(settings),
        coupling=str(coupling_setting.value),
        correction_latency=correction_latency,
        camilla_config_hash=camilla_config_hash,
    )
    combined_plan_warnings = tuple(plan_warnings) + route_profile.warnings
    return AudioRuntimePlan(
        profile_id=profile_id or "unknown",
        profile_label=profile.label if profile is not None else "unknown",
        route_mode=route_mode if route_mode in _VALID_ROUTE_MODES else "unknown",
        settings=tuple(settings),
        coupling_support=support,
        transport_topology=topology,
        route_profile=route_profile,
        route_config_hash=route_hash,
        camilla_config_hash=camilla_config_hash,
        correction_latency_eligibility=correction_latency,
        route_policy_errors=_route_policy_errors(
            route=route_profile,
            coupling=str(coupling_setting.value),
            outputd_env=outputd_values,
            camilla_devices=camilla_devices,
        ),
        plan_warnings=combined_plan_warnings,
    )


def transport_topology_for_coupling(
    coupling: str | None,
    *,
    fanin_env: Mapping[str, str] | None = None,
    outputd_env: Mapping[str, str] | None = None,
    camilla_playback_device: str | None = None,
) -> TransportTopology:
    """Return the concrete transport topology implied by the coupling intent."""

    fanin_values = dict(fanin_env or {})
    outputd_values = dict(outputd_env or {})
    normalized = resolve_coupling(coupling)
    if normalized == COUPLING_SHM_RING:
        # Ring A (fan-in -> CamillaDSP, jts_ring_capture) + Ring B (CamillaDSP ->
        # outputd, jts_ring_playback). Both S16_LE ALSA ioplug devices; the
        # concrete ring paths live in the daemons' env (fan-in RING_PATH, outputd
        # SHM_RING_PATH), surfaced here so /state names the resolved transport.
        from jasper.fanin_coupling import (
            OUTPUTD_RING_PATH_ENV_VAR,
            RING_CAPTURE_DEVICE,
            RING_PATH_ENV_VAR,
            RING_PLAYBACK_DEVICE,
            RING_WIRE_FORMAT,
            resolve_outputd_ring_path,
            resolve_ring_path,
        )

        capture_ring = resolve_ring_path(fanin_values.get(RING_PATH_ENV_VAR))
        content_ring = resolve_outputd_ring_path(
            outputd_values.get(OUTPUTD_RING_PATH_ENV_VAR)
        )
        return TransportTopology(
            name=COUPLING_SHM_RING,
            fanin_to_camilla={
                "transport": "shm_ring",
                "path": capture_ring,
                "writer": "jasper-fanin",
                "camilla_capture_device": RING_CAPTURE_DEVICE,
                "format": RING_WIRE_FORMAT,
                "channels": 2,
                "sample_rate": DEFAULT_SAMPLE_RATE,
            },
            camilla_to_outputd={
                "transport": "shm_ring",
                "path": content_ring,
                "camilla_playback_device": RING_PLAYBACK_DEVICE,
                "reader": "jasper-outputd",
                "format": RING_WIRE_FORMAT,
                "channels": 2,
                "sample_rate": DEFAULT_SAMPLE_RATE,
            },
            camilla={
                "chunksize": RING_CAMILLA_CHUNKSIZE,
                "target_level": RING_CAMILLA_TARGET_LEVEL,
                "queuelimit": RING_CAMILLA_QUEUELIMIT,
                # The one-clock ring graph is already DAC-paced by outputd's
                # blocking read/write chain; the validated chunk-128 PoC ran
                # rate_adjust off, while the rate_adjust-on Ring A+B lesson
                # packed the queues and measured ~194 ms.
                "enable_rate_adjust": RING_CAMILLA_ENABLE_RATE_ADJUST,
                "capture_resampler": None,
            },
            outputd_content_source="shm_ring",
        )
    loopback_playback = camilla_playback_device or DEFAULT_PLAYBACK_DEVICE
    loopback_capture = (
        outputd_capture_device_for_playback(loopback_playback)
        or outputd_capture_device_for_playback(DEFAULT_PLAYBACK_DEVICE)
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
            "camilla_playback_device": loopback_playback,
            "outputd_capture_pcm": loopback_capture,
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


def transport_coherence_errors(
    *,
    coupling: str | None,
    outputd_env: Mapping[str, str] | None = None,
    camilla_devices: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Return contradictions across the complete Camilla/outputd transport.

    ``TransportTopology`` is the policy source. This function compares its two
    runtime consumers without re-deriving endpoint strings in reconcilers or
    doctor checks. Missing Camilla evidence is not itself an error; a concrete
    contradiction is.
    """

    outputd_values = dict(outputd_env or {})
    devices = dict(camilla_devices or {})
    playback_device = str(devices.get("playback_device") or "") or None
    capture_device = str(devices.get("capture_device") or "") or None
    topology = transport_topology_for_coupling(
        coupling,
        outputd_env=outputd_values,
        camilla_playback_device=playback_device,
    )
    errors: list[str] = []
    normalized = topology.name
    raw_bridge = str(
        outputd_values.get(OUTPUTD_CONTENT_BRIDGE_KEY, OUTPUTD_CONTENT_BRIDGE_DIRECT)
        or OUTPUTD_CONTENT_BRIDGE_DIRECT
    ).strip().lower()

    if normalized == COUPLING_SHM_RING:
        expected_capture = str(
            topology.fanin_to_camilla.get("camilla_capture_device") or ""
        )
        expected_playback = str(
            topology.camilla_to_outputd.get("camilla_playback_device") or ""
        )
        if raw_bridge != COUPLING_SHM_RING:
            errors.append(
                f"transport plan is shm_ring but {OUTPUTD_CONTENT_BRIDGE_KEY}="
                f"{raw_bridge}; Ring A and Ring B must move together"
            )
        if capture_device and capture_device != expected_capture:
            errors.append(
                f"transport plan is shm_ring but Camilla capture={capture_device!r}; "
                f"expected {expected_capture!r}"
            )
        if playback_device and playback_device != expected_playback:
            errors.append(
                f"transport plan is shm_ring but Camilla playback={playback_device!r}; "
                f"expected {expected_playback!r}"
            )
        return tuple(errors)

    if raw_bridge == COUPLING_SHM_RING:
        errors.append(
            f"transport plan is {normalized} but {OUTPUTD_CONTENT_BRIDGE_KEY}=shm_ring; "
            "Ring B is armed without the matching Ring A plan"
        )

    if normalized == COUPLING_LOOPBACK and playback_device:
        paired_capture = outputd_capture_device_for_playback(playback_device)
        if paired_capture is not None:
            actual_capture = str(
                outputd_values.get(
                    "JASPER_OUTPUTD_CONTENT_PCM",
                    outputd_capture_device_for_playback(DEFAULT_PLAYBACK_DEVICE),
                )
                or ""
            )
            if actual_capture != paired_capture:
                errors.append(
                    f"post-DSP route disconnected: Camilla playback={playback_device!r} "
                    f"requires outputd capture={paired_capture!r}, got "
                    f"{actual_capture!r}"
                )
        elif playback_device == ACTIVE_OUTPUTD_PLAYBACK_DEVICE:
            # Defensive completeness if the pairing registry is edited without
            # updating this plan layer.
            errors.append(
                f"post-DSP route has no registered outputd capture for "
                f"Camilla playback={playback_device!r}"
            )
    return tuple(errors)


def output_endpoint_devices_from_statefiles(
    *paths: str | Path,
) -> dict[str, Any] | None:
    """Compatibility wrapper returning only loaded endpoint devices."""

    evidence = output_endpoint_evidence_from_statefiles(*paths)
    return dict(evidence.devices) if evidence.devices is not None else None


def output_endpoint_evidence_from_statefiles(
    *paths: str | Path,
) -> OutputEndpointEvidence:
    """Return the loaded Camilla graph that actually feeds outputd.

    Active leaders keep a program-bake graph in the primary statefile and the
    driver/outputd endpoint in the crossover statefile. Inspect both in order
    and select the first recognized output endpoint, using the same vocabulary
    as :class:`TransportTopology`.
    """

    from jasper.active_speaker.environment import parse_camilla_statefile_config_path
    from jasper.fanin_coupling import RING_PLAYBACK_DEVICE

    fallback: dict[str, Any] | None = None
    errors: list[str] = []
    output_endpoints = {
        DEFAULT_PLAYBACK_DEVICE,
        ACTIVE_OUTPUTD_PLAYBACK_DEVICE,
        RING_PLAYBACK_DEVICE,
    }
    for statefile_path in paths:
        try:
            statefile_text = Path(statefile_path).read_text(encoding="utf-8")
        except OSError as e:
            errors.append(f"statefile {statefile_path}: {e.strerror or type(e).__name__}")
            continue
        config_path = parse_camilla_statefile_config_path(statefile_text)
        if not config_path:
            errors.append(f"statefile {statefile_path}: config_path missing")
            continue
        devices = read_camilla_devices_config(config_path)
        if devices is None:
            errors.append(f"CamillaDSP config {config_path}: devices unavailable")
            continue
        if fallback is None:
            fallback = devices
        if devices.get("playback_device") in output_endpoints:
            return OutputEndpointEvidence(
                devices=devices,
                errors=tuple(errors),
                endpoint_recognized=True,
            )
    return OutputEndpointEvidence(
        devices=fallback,
        errors=tuple(errors),
        endpoint_recognized=False,
    )


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

    base_values = dict(base_env or {})
    override_values = dict(overrides or {})
    profile = (profile_id or "").strip()
    plan = build_audio_runtime_plan(
        profile_id=profile,
        base_env=base_values,
        outputd_env=outputd_env,
        overrides=override_values,
        route_mode="solo",
    )

    actions: list[RuntimeEnvAction] = []
    for key in OUTPUTD_LATENCY_KEYS:
        override_value, _ = _positive_int(_raw(override_values, key))
        setting = plan.setting(key)
        if override_value is not None:
            actions.append(RuntimeEnvAction("set", key, str(setting.value)))
        elif key in base_values:
            actions.append(RuntimeEnvAction("unset", key))
        elif setting.source_kind in {"device_profile", "route_policy"}:
            if key == OUTPUTD_CONTENT_BUFFER_KEY and int(setting.value) == (
                DEFAULT_OUTPUTD_CONTENT_BUFFER_FRAMES
            ):
                actions.append(RuntimeEnvAction("unset", key))
            else:
                actions.append(RuntimeEnvAction("set", key, str(setting.value)))
        else:
            actions.append(RuntimeEnvAction("unset", key))
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


def fanin_coupling_capture_kwargs(
    coupling: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> EmitSoundConfigKwargs:
    """Return CamillaDSP capture kwargs for the shared fan-in coupling.

    ``coupling=None`` means read the live env, matching ordinary sound/correction
    emits. An explicit coupling is used by the coupling reconciler immediately
    after it rewrites ``fanin.env``; process env may still be stale, so the
    explicit value wins.

    **Coupling TOKEN is resolved FILE-FRESH on the live path** (``coupling is
    None`` AND ``env is None``): we delegate to
    :func:`coupling_capture_kwargs_from_env` with ``env=None`` so it reads the
    persisted ``fanin.env`` SSOT (:func:`read_persisted_coupling`) rather than a
    STALE ``os.environ``. This is the same ``os.environ``-stale class the #1158
    Blocker-1 fix closed for the socket-activated wizards (``/sound/`` /
    ``/correction/``) — but that fix lived inside
    ``coupling_capture_kwargs_from_env``, and this helper previously synthesized
    ``dict(os.environ)`` unconditionally, forcing the explicit-env branch and
    defeating it on the CLI/install ``jasper-sound reconcile-current-dsp`` path
    (which only ``load_env_files()``-hydrates via ``setdefault`` and is NOT the
    reconciler's pre-synced-env caller). On a loopback box a polluted
    ``os.environ`` coupling then emitted a RING capture/playback config —
    CamillaDSP crash-loops on a ring nobody writes (hardware-reproduced on
    jts.local). An EXPLICIT ``env`` mapping stays authoritative (no file read) for
    callers that want to pin the resolution deterministically — today only unit
    tests, which pass a controlled env. No production caller passes ``env=``: the
    reconciler/binder pre-syncs ``os.environ`` and the coupling files, then relies
    on the ``coupling is None, env is None`` file-fresh path here (the token is
    read from the ``fanin.env`` it just wrote), so the live emit never depends on a
    stale ``os.environ`` coupling token.
    """

    if coupling is None:
        # Live path (env is None): file-fresh coupling token. Explicit env:
        # authoritative, no file read — pass the mapping straight through.
        return cast(
            EmitSoundConfigKwargs,
            coupling_capture_kwargs_from_env(None if env is None else dict(env)),
        )
    return cast(
        EmitSoundConfigKwargs,
        capture_kwargs_for_coupling(coupling),
    )


def apply_capture_precedence(
    emit_kwargs: Mapping[str, object],
    fanin_coupling_capture_kwargs: Mapping[str, object] | None,
    *,
    member_kwargs: Mapping[str, object] | None,
) -> EmitSoundConfigKwargs:
    """Apply capture-precedence policy to an ``emit_sound_config`` kwargs dict.

    The shared fan-in transport coupling applies only when no more-specific
    topology already owns this emit:

    - a grouped/member pipe sink wins because its Snapcast playback pipe is
      mutually exclusive with the local Camilla -> outputd playback pipe;
    - otherwise the fan-in coupling kwargs overwrite the local Camilla capture
      and playback transport keys together.

    Empty coupling kwargs keep the input kwargs unchanged apart from returning a
    detached ``dict`` for callers to mutate safely.
    """

    if (
        not fanin_coupling_capture_kwargs
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


def _effective_outputd_positive_int(
    key: str,
    *,
    default: int,
    layers: Sequence[Mapping[str, str]],
) -> tuple[int, str | None]:
    for env in reversed(layers):
        raw = _raw(env, key)
        if raw is None:
            continue
        value, error = _positive_int(raw)
        if error is not None or value is None:
            return default, f"{key}={raw!r} is invalid ({error})"
        return value, None
    return default, None


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


def _resolve_outputd_content_buffer_int(
    *,
    route: AudioRouteProfile,
    base_env: Mapping[str, str],
    override_env: Mapping[str, str],
    generated_env: Mapping[str, str],
    base_label: str,
    override_label: str,
    generated_label: str,
) -> RuntimeSetting:
    key = OUTPUTD_CONTENT_BUFFER_KEY
    operator_raw = _raw(base_env, key)
    override_raw = _raw(override_env, key)
    generated_raw = _raw(generated_env, key)
    operator_value, operator_error = _positive_int(operator_raw)
    override_value, override_error = _positive_int(override_raw)
    generated_value, generated_error = _positive_int(generated_raw)
    # The low-latency route policy for the outputd content buffer is architecturally
    # INERT under the shm_ring content bridge (Ring B): outputd never opens the
    # content ALSA PCM, so `configure_pcm` — the only consumer of
    # JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES — is skipped (alsa_backend.rs). Emitting
    # 1536 there is one-knob-two-truths drift: the env says 1536 while the honest
    # /state ring capacity is n_slots x period. Drop the route policy under shm_ring
    # so the reconciler unsets the key (falling back to outputd's compile-time
    # default, equally inert but non-misleading). Keyed on the OUTPUTD BRIDGE read
    # from generated_env (outputd.env) — the value the daemon actually consumes to
    # decide whether to open the content PCM — NOT the coupling: the writer-side
    # `outputd_latency_floor_actions` path does not thread fanin.env, so the coupling
    # is invisible there, but the outputd bridge is always in outputd.env. Uses the
    # fail-safe resolver so only a genuine `shm_ring` (never `rate_match`, which DOES
    # open the content PCM, or a garbage value) suppresses the policy.
    generated_bridge = resolve_outputd_content_bridge(
        _raw(generated_env, OUTPUTD_CONTENT_BRIDGE_KEY)
    )
    route_value = (
        DEFAULT_USB_LOW_LATENCY_OUTPUTD_CONTENT_BUFFER_FRAMES
        if route.route_id == ROUTE_USB_LOW_LATENCY_48K
        and generated_bridge != OUTPUTD_CONTENT_BRIDGE_SHM_RING
        else None
    )
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
            "wins over env/route values"
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

    if route_value is not None:
        if generated_value is None:
            warnings.append(
                f"{key} route policy for {route.route_id} is {route_value}, but "
                f"{generated_label} does not emit it; run "
                "jasper-audio-hardware-reconcile"
            )
        elif generated_value != route_value:
            warnings.append(
                f"{key} in {generated_label} is {generated_value}, but the "
                f"{route.route_id} route policy is {route_value}; rerun "
                "audio hardware reconcile"
            )
        return RuntimeSetting(
            key=key,
            value=route_value,
            source_kind="route_policy",
            source=f"AudioRouteProfile:{route.route_id}",
            unit="frames",
            operator_value=operator_raw,
            generated_value=generated_raw,
            warnings=tuple(warnings),
        )

    if generated_value is not None and generated_value != DEFAULT_OUTPUTD_CONTENT_BUFFER_FRAMES:
        warnings.append(
            f"{key} in {generated_label} is {generated_value}, but the active "
            f"route has no content-buffer policy; stale generated value will "
            f"override the packaged default {DEFAULT_OUTPUTD_CONTENT_BUFFER_FRAMES}"
        )
    return RuntimeSetting(
        key=key,
        value=DEFAULT_OUTPUTD_CONTENT_BUFFER_FRAMES,
        source_kind="packaged_default",
        source="packaged systemd/outputd default",
        unit="frames",
        operator_value=operator_raw,
        generated_value=generated_raw,
        warnings=tuple(warnings),
    )


def _coherent_outputd_content_buffer_setting(
    *,
    period_setting: RuntimeSetting,
    content_buffer_setting: RuntimeSetting,
) -> RuntimeSetting:
    period_frames = int(period_setting.value)
    content_buffer_frames = int(content_buffer_setting.value)
    detail = outputd_content_buffer_pair_error(
        period_frames=period_frames,
        content_buffer_frames=content_buffer_frames,
    )
    if detail is None:
        return content_buffer_setting
    if content_buffer_setting.source_kind != "route_policy":
        return replace(
            content_buffer_setting,
            warnings=content_buffer_setting.warnings
            + (
                f"{detail}; {content_buffer_setting.key} comes from "
                f"{content_buffer_setting.source}, so the reconciler will refuse "
                "to write this candidate until the pair is coherent",
            ),
        )

    repaired_value = max(
        DEFAULT_OUTPUTD_CONTENT_BUFFER_FRAMES,
        minimum_outputd_buffer_frames(period_frames),
    )
    source_kind: SourceKind = (
        "packaged_default"
        if repaired_value == DEFAULT_OUTPUTD_CONTENT_BUFFER_FRAMES
        else "route_policy"
    )
    source = (
        "packaged systemd/outputd default"
        if source_kind == "packaged_default"
        else content_buffer_setting.source
    )
    return replace(
        content_buffer_setting,
        value=repaired_value,
        source_kind=source_kind,
        source=source,
        warnings=content_buffer_setting.warnings
        + (
            f"{detail}; suppressing the low-latency route buffer until "
            f"{OUTPUTD_PERIOD_KEY} is coherent, using "
            f"{content_buffer_setting.key}={repaired_value}",
        ),
    )


def _effective_camilla_target_setting(
    *,
    target_setting: RuntimeSetting,
    coupling: str,
) -> RuntimeSetting:
    """Return the Camilla target that generated YAML actually emits.

    In the ordinary ALSA loopback topology, CamillaDSP's target_level is a real
    playback-buffer latency/stability knob. Under shm_ring, the emitter uses the
    validated ring geometry (target 128) instead of the loopback DAC floor. Keep
    the route plan/hash on those same effective values so latency artifacts match
    the loaded graph instead of the generated env floor used by loopback profiles.
    """

    normalized = resolve_coupling(coupling)
    if normalized == COUPLING_SHM_RING:
        if (
            target_setting.value == RING_CAMILLA_TARGET_LEVEL
            and target_setting.source_kind == "route_policy"
        ):
            return target_setting
        warnings = list(target_setting.warnings)
        if target_setting.value != RING_CAMILLA_TARGET_LEVEL:
            warnings.append(
                "JASPER_CAMILLA_TARGET_LEVEL effective value is "
                f"{RING_CAMILLA_TARGET_LEVEL} under shm_ring; "
                f"{target_setting.value} from {target_setting.source} is the "
                "loopback/hardware-floor value, not the ring runtime value"
            )
        return RuntimeSetting(
            key=target_setting.key,
            value=RING_CAMILLA_TARGET_LEVEL,
            source_kind="route_policy",
            source="shm_ring validated ring geometry",
            unit=target_setting.unit,
            override_value=target_setting.override_value,
            generated_value=target_setting.generated_value,
            operator_value=target_setting.operator_value,
            warnings=tuple(warnings),
        )
    return target_setting


def _effective_camilla_chunksize_setting(
    *,
    chunksize_setting: RuntimeSetting,
    coupling: str,
) -> RuntimeSetting:
    """Return the Camilla chunksize generated YAML actually emits."""

    if resolve_coupling(coupling) != COUPLING_SHM_RING:
        return chunksize_setting
    if (
        chunksize_setting.value == RING_CAMILLA_CHUNKSIZE
        and chunksize_setting.source_kind == "route_policy"
    ):
        return chunksize_setting
    warnings = list(chunksize_setting.warnings)
    if chunksize_setting.value != RING_CAMILLA_CHUNKSIZE:
        warnings.append(
            "JASPER_CAMILLA_CHUNKSIZE effective value is "
            f"{RING_CAMILLA_CHUNKSIZE} under shm_ring; "
            f"{chunksize_setting.value} from {chunksize_setting.source} is the "
            "loopback/hardware-floor value, not the ring runtime value"
        )
    return RuntimeSetting(
        key=chunksize_setting.key,
        value=RING_CAMILLA_CHUNKSIZE,
        source_kind="route_policy",
        source="shm_ring validated ring geometry",
        unit=chunksize_setting.unit,
        override_value=chunksize_setting.override_value,
        generated_value=chunksize_setting.generated_value,
        operator_value=chunksize_setting.operator_value,
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
    operator_env_allowed: bool = False,
    min_value: int = 1,
    max_value: int | None = None,
) -> RuntimeSetting:
    operator_raw = _raw(base_env, key)
    override_raw = _raw(override_env, key)
    generated_raw = _raw(fanin_env, key)
    operator_value, operator_error = _positive_int(operator_raw)
    override_value, override_error = _positive_int(override_raw)
    generated_value, generated_error = _positive_int(generated_raw)

    def enforce_bounds(
        value: int | None,
        error: str | None,
    ) -> tuple[int | None, str | None]:
        if value is None or error is not None:
            return value, error
        if value < min_value or (max_value is not None and value > max_value):
            upper = f"..{max_value}" if max_value is not None else " or greater"
            return None, f"must be {min_value}{upper}"
        return value, None

    operator_value, operator_error = enforce_bounds(
        operator_value, operator_error,
    )
    override_value, override_error = enforce_bounds(
        override_value, override_error,
    )
    generated_value, generated_error = enforce_bounds(
        generated_value, generated_error,
    )
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
    if operator_raw is not None and not operator_env_allowed:
        warnings.append(
            f"{key} is present in {base_label}; fan-in tuning belongs in "
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
