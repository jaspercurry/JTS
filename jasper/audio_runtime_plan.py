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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, TypedDict, cast

from jasper.audio_hardware.dac import by_id as dac_profile_by_id
from jasper.audio_hardware.dac import camilla_floor_for, latency_floor_for
from jasper.audio_runtime_overrides import (
    DEFAULT_AUDIO_RUNTIME_OVERRIDES_PATH,
    RuntimeOverrideEntry,
    load_runtime_overrides,
    runtime_overrides_path,
)
from jasper.camilla_config_contract import (
    ACTIVE_OUTPUTD_PLAYBACK_DEVICE,
    DEFAULT_CHUNKSIZE,
    DEFAULT_PLAYBACK_DEVICE,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_TARGET_LEVEL,
    read_camilla_devices_config,
)
from jasper.env_load import read_env_file_state
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_SHM_RING,
    OUTPUTD_CONTENT_BRIDGE_SHM_RING,
    capture_half,
    coupling_value_removed,
    dac_content_lane_marker_armed,
    outputd_content_is_central_ring,
    resolve_coupling,
)

# The named transport SHAPES. ``TransportTopology.name`` is the discriminator
# every consumer matches on, so each distinct transport gets its own name rather
# than a shared name with a device threaded through it: an exhaustive match over
# named shapes fails LOUD on one nobody handled, where a threaded device value
# shears silently through five call sites.
#
# ``shm_ring_active`` is selected on the PERSISTED COUPLING plus the reconciler's
# endpoint MARKER — deliberately NOT on the observed ``camilla_playback_device``.
# Selecting on the observed device would make
# :func:`jasper.transport_coherence.transport_coherence_report`' playback
# comparison vacuous: it would derive the expectation from the very value it is
# checking, so a Camilla graph pointed at the wrong ring would define itself
# correct.
TRANSPORT_SHM_RING = COUPLING_SHM_RING
TRANSPORT_SHM_RING_ACTIVE = "shm_ring_active"
# One END of the box is off the one transport (ADR-0100) — the LEGACY FIFO
# spelling of the round-trip ``dac_content`` lane, which outputd requires
# ``CONTENT_BRIDGE=direct`` for, or a coupling/bridge a daemon parks on. Not a
# second route: jasper.control.transport_park is what names such a box. The
# ring MARKER's shape is NOT this one — see TRANSPORT_DAC_CONTENT_RING below,
# which is served.
TRANSPORT_OFF_RING = "off_ring"
# A DUMB bonded member: outputd's content comes off the dac-content RETURN ring
# and no CENTRAL post-DSP ring is attached at all. Its own shape rather than
# TRANSPORT_OFF_RING, which would drop a healthy bonded member into the arm
# whose comparisons assume nothing is feeding outputd — while Ring A is still
# live on this box and must keep being compared.
TRANSPORT_DAC_CONTENT_RING = "dac_content_ring"
# Every named shape, so an exhaustive consumer can assert it handled one.
TRANSPORT_SHAPES = frozenset(
    (
        TRANSPORT_OFF_RING,
        TRANSPORT_SHM_RING,
        TRANSPORT_SHM_RING_ACTIVE,
        TRANSPORT_DAC_CONTENT_RING,
    )
)


DEFAULT_BASE_ENV_PATH = "/etc/jasper/jasper.env"
DEFAULT_OUTPUTD_ENV_PATH = "/var/lib/jasper/outputd.env"
DEFAULT_FANIN_ENV_PATH = "/var/lib/jasper/fanin.env"
DEFAULT_GROUPING_ENV_PATH = "/var/lib/jasper/grouping.env"
DEFAULT_CAMILLA_STATEFILE_PATH = "/var/lib/camilladsp/outputd-statefile.yml"
DEFAULT_CAMILLA2_STATEFILE_PATH = "/var/lib/camilladsp/crossover-statefile.yml"

OUTPUTD_PERIOD_KEY = "JASPER_OUTPUTD_PERIOD_FRAMES"
OUTPUTD_DAC_BUFFER_KEY = "JASPER_OUTPUTD_DAC_BUFFER_FRAMES"
OUTPUTD_MIN_BUFFER_PERIOD_MULTIPLIER = 2
DEFAULT_OUTPUTD_PERIOD_FRAMES = 1024
DEFAULT_OUTPUTD_DAC_BUFFER_FRAMES = 3072
# How the two defaults above are NAMED to an operator: nothing writes them, so
# there is no file to edit — they are outputd's own compile-time defaults
# (rust/jasper-outputd/src/config.rs, pinned equal by
# test_packaged_outputd_defaults_match_the_rust_daemon).
PACKAGED_OUTPUTD_DEFAULT_SOURCE = "packaged outputd default"
OUTPUTD_CONTENT_BRIDGE_KEY = "JASPER_OUTPUTD_CONTENT_BRIDGE"
# The width outputd assumes on its content upstream when
# JASPER_OUTPUTD_CONTENT_FORMAT is absent or empty: outputd's own documented
# default, the pre-flip S16 lane, NOT whatever the resolver would pick for this
# box. A reader that follows the resolver here would refuse an arm for a wire
# the daemon has in fact declared.
OUTPUTD_DEFAULT_CONTENT_FORMAT = "S16_LE"
MAX_LOW_LATENCY_CORRECTION_GROUP_DELAY_FRAMES = 512
FANIN_INPUT_BUFFER_KEY = "JASPER_FANIN_INPUT_BUFFER_FRAMES"
DEFAULT_FANIN_INPUT_BUFFER_FRAMES = 4096
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
AUDIO_ROUTE_PROFILE_KEY = "JASPER_AUDIO_ROUTE_PROFILE"
ROUTE_CORRECTED_48K = "corrected_48k"
ROUTE_USB_LOW_LATENCY_48K = "usb_low_latency_48k"
ROUTE_BITPERFECT_DECLARED = "bitperfect_passthrough_declared"
USB_LOW_LATENCY_SOURCE_ID = "usbsink"
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
    OUTPUTD_DAC_BUFFER_KEY,
)
AUDIO_RUNTIME_OVERRIDE_KEYS = frozenset(
    OUTPUTD_LATENCY_KEYS
    + (FANIN_INPUT_BUFFER_KEY,)
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

SourceKind = Literal[
    "operator_env",
    "generated_env",
    "device_profile",
    "packaged_default",
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
_VALID_AUDIO_ROUTE_PROFILES = {
    ROUTE_CORRECTED_48K,
    ROUTE_USB_LOW_LATENCY_48K,
    ROUTE_BITPERFECT_DECLARED,
}


class EmitSoundConfigKwargs(TypedDict, total=False):
    """Subset of ``emit_sound_config`` kwargs owned by runtime routing."""

    room_peqs_right: Any
    channel_delays_ms: Any
    playback_pipe_path: str | None
    # Ring (shm_ring) coupling names its CamillaDSP capture/playback devices via
    # ALSA ioplug devices (jts_ring_capture, plus jts_ring_playback or — on an
    # armed roleful box — jts_ring_active_playback), so BOTH device and format
    # ride the coupling kwargs.
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
class EmittedCamillaGeometry:
    """What the LOADED CamillaDSP config declares — read, never derived.

    A DIFFERENT fact from the plan's ``JASPER_CAMILLA_*`` settings, which answer
    what an emitter's fallback WOULD resolve. The two legitimately differ: a
    graph built end-to-end on the ring passes
    :data:`~jasper.fanin_coupling.RING_CAMILLA_GEOMETRY` explicitly, and an
    ordinary graph's chunk is clamped to the ring's capacity by
    ``resolve_camilla_latency_for_devices``. A surface that reports only the
    settings therefore names a geometry no config on the box need carry.
    """

    config_path: str
    chunksize: int | None
    target_level: int | None
    capture_device: str | None
    playback_device: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_path": self.config_path,
            "chunksize": self.chunksize,
            "target_level": self.target_level,
            "capture_device": self.capture_device,
            "playback_device": self.playback_device,
        }


def _emitted_camilla_geometry(
    config_path: str | None,
    camilla_devices: Mapping[str, Any] | None,
) -> EmittedCamillaGeometry | None:
    if not config_path or not camilla_devices:
        return None

    def _int(key: str) -> int | None:
        value = camilla_devices.get(key)
        return value if isinstance(value, int) else None

    def _text(key: str) -> str | None:
        value = camilla_devices.get(key)
        return value if isinstance(value, str) and value else None

    return EmittedCamillaGeometry(
        config_path=config_path,
        chunksize=_int("chunksize"),
        target_level=_int("target_level"),
        capture_device=_text("capture_device"),
        playback_device=_text("playback_device"),
    )


@dataclass(frozen=True)
class AudioRuntimePlan:
    """Resolved audio settings plus route-policy errors.

    ``settings`` is POLICY — what the layered env/floor resolution answers, i.e.
    what an emitter's fallback would read. ``camilla_emitted`` is OBSERVATION —
    what the loaded config declares. Distinct facts that legitimately differ; see
    :class:`EmittedCamillaGeometry`.
    """

    profile_id: str
    profile_label: str
    route_mode: RouteMode
    settings: tuple[RuntimeSetting, ...]
    transport_topology: TransportTopology
    route_profile: AudioRouteProfile
    route_config_hash: str
    camilla_config_hash: str
    correction_latency_eligibility: CorrectionLatencyEligibility
    #: jasper-outputd's env as its unit layers it, so a consumer that needs the
    #: running transport's markers reads what the plan read instead of merging
    #: the two files again. Inputs, not decisions: deliberately not in
    #: :meth:`to_dict`.
    outputd_env: Mapping[str, str] = field(default_factory=dict)
    route_policy_errors: tuple[str, ...] = ()
    #: One stable token per entry in :attr:`route_policy_errors`, same order and
    #: same length. Tests and future consumers branch on THESE; the strings
    #: beside them are operator prose and may be rewritten freely.
    route_policy_reason_codes: tuple[str, ...] = ()
    plan_warnings: tuple[str, ...] = ()
    camilla_emitted: EmittedCamillaGeometry | None = None

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
            "transport_topology": self.transport_topology.to_dict(),
            "route_profile": self.route_profile.to_dict(),
            "route_config_hash": self.route_config_hash,
            "camilla_config_hash": self.camilla_config_hash,
            "camilla_emitted": (
                self.camilla_emitted.to_dict()
                if self.camilla_emitted is not None
                else None
            ),
            "route_latency_identity": self.route_latency_identity(),
            "correction_latency_eligibility": (
                self.correction_latency_eligibility.to_dict()
            ),
            "route_policy_errors": list(self.route_policy_errors),
            "route_policy_reason_codes": list(self.route_policy_reason_codes),
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


OUTPUTD_ENV_LAYER = 1


def _pair_provenance(
    *,
    buffer_key: str,
    buffer_frames: int,
    buffer_layer: int | None,
    period_frames: int,
    period_layer: int | None,
    labels: tuple[str, str],
    override_entries: Mapping[str, RuntimeOverrideEntry],
    override_label: str,
) -> str:
    """Name where each half of a failing buffer/period pair actually came from.

    The Rust-shaped detail names the two KEYS, which is enough for the daemon
    (it reads one merged environment) and not enough for an operator, who has to
    edit one of several layers. Naming the layer is what turns the refusal into
    an action, and it matters most in the two cases that read as a
    contradiction:

    - the reconciler UNSETS a key from ``outputd.env`` whenever ``jasper.env``
      owns it, so an operator told only "this key is wrong" looks in the
      reconciler-owned file, finds the key absent, and is stuck;
    - a value the LAB OVERRIDE STORE owns is WRITTEN INTO ``outputd.env`` by the
      latency-floor pass (``outputd_latency_floor_actions`` emits ``set`` when
      the store holds the key), so deleting that line is futile — the next
      reconcile writes it straight back. Naming only the file would send an
      operator into exactly that loop.

    Neither is hypothetical: the second is jts.local's live #2489 state, whose
    store entry carries its own ``created_at`` and ``reason``. Those are quoted
    here because they are the self-explanation that resolves the case on sight.
    """

    def store_entry(key: str, frames: int, layer: int | None) -> RuntimeOverrideEntry | None:
        """The store entry that EXPLAINS this value, or None.

        Attribution requires the value to match: a store entry that disagrees
        with what is on disk describes a DIFFERENT value, and claiming it as the
        origin would be a wrong attribution rather than a missing one.
        """
        if layer != OUTPUTD_ENV_LAYER:
            return None
        entry = override_entries.get(key)
        if entry is None or entry.value.strip() != str(frames):
            return None
        return entry

    def where(key: str, frames: int, layer: int | None) -> str:
        if layer is None:
            return PACKAGED_OUTPUTD_DEFAULT_SOURCE
        entry = store_entry(key, frames, layer)
        if entry is None:
            return labels[layer]
        fields = ", ".join(
            f"{name}={value}"
            for name, value in (("created_at", entry.created_at), ("reason", entry.reason))
            if value
        )
        # The store path the caller ACTUALLY read, never the production
        # constant: naming a file that was not consulted is the wrong-origin
        # failure this provenance exists to prevent.
        origin = (
            f"{labels[layer]}, written there from the override store {override_label}"
        )
        return f"{origin} ({fields})" if fields else origin

    halves = (
        (buffer_key, buffer_frames, buffer_layer),
        (OUTPUTD_PERIOD_KEY, period_frames, period_layer),
    )
    detail = ", ".join(
        f"{key}={frames} comes from {where(key, frames, layer)}"
        for key, frames, layer in halves
    )
    # At least one half is always layer-owned, because the packaged defaults are
    # mutually coherent by contract
    # (test_packaged_outputd_buffer_defaults_are_mutually_coherent) — so there is
    # always a named source here, and no "this is a build defect" branch to carry
    # at runtime.
    stored = [key for key, frames, layer in halves if store_entry(key, frames, layer)]
    if stored:
        # Name the actual key(s), not a `<key>` placeholder the operator has to
        # translate — the remediation should be runnable as printed.
        clear = " && ".join(f"jasper-audio-config overrides-clear {key}" for key in stored)
        return (
            f"{detail}; clearing the {labels[OUTPUTD_ENV_LAYER]} line alone is undone by "
            f"the next reconcile — clear the override with `{clear}`"
        )
    return (
        f"{detail}; correct or remove the losing line in the file named above — "
        "the reconciler will refuse to write this candidate until the pair is coherent"
    )


def outputd_env_buffer_pair_error(
    *,
    base_env: Mapping[str, str] | None = None,
    outputd_env: Mapping[str, str] | None = None,
    base_label: str | None = None,
    outputd_label: str | None = None,
    override_entries: Mapping[str, RuntimeOverrideEntry] | None = None,
    override_label: str = DEFAULT_AUDIO_RUNTIME_OVERRIDES_PATH,
) -> str | None:
    """Validate effective outputd buffer/period pairs for env-file writers.

    Precedence mirrors the service contract: packaged defaults, then
    ``/etc/jasper/jasper.env``, then the reconciler-owned ``outputd.env``.
    The check order mirrors Rust's outputd config validator so logs name the
    same first failing pair the daemon would reject with EX_CONFIG.

    Pass BOTH ``base_label`` and ``outputd_label`` — the paths the two mappings
    were read from — to append :func:`_pair_provenance`, which names the layer
    each half of the failing pair came from. With either omitted the returned
    string stays the bare Rust mirror, byte for byte, which is what
    ``test_python_outputd_buffer_contract_matches_rust_validator`` compares
    against the daemon's own message. Callers that HAVE the paths should always
    pass them: the mirror alone cannot tell an operator which file to edit.

    ``override_entries`` (the lab override store, keyed by env key) is what lets
    the provenance distinguish a line an operator wrote in ``outputd.env`` from
    one the latency-floor pass copied there out of the store. The store is NOT a
    precedence layer here — outputd never reads it — but it is the ORIGIN of
    some values that reach ``outputd.env``, and only naming it makes the refusal
    actionable.
    """

    values = [dict(base_env or {}), dict(outputd_env or {})]
    labels = (
        (base_label, outputd_label)
        if base_label is not None and outputd_label is not None
        else None
    )
    entries = dict(override_entries or {})
    period_frames, period_error, period_layer = _effective_outputd_positive_int(
        OUTPUTD_PERIOD_KEY,
        default=DEFAULT_OUTPUTD_PERIOD_FRAMES,
        layers=values,
    )
    if period_error is not None:
        return period_error
    dac_buffer_frames, dac_error, dac_layer = _effective_outputd_positive_int(
        OUTPUTD_DAC_BUFFER_KEY,
        default=DEFAULT_OUTPUTD_DAC_BUFFER_FRAMES,
        layers=values,
    )
    if dac_error is not None:
        return dac_error
    detail = outputd_dac_buffer_pair_error(
        period_frames=period_frames,
        dac_buffer_frames=dac_buffer_frames,
    )
    if detail is None or labels is None:
        return detail
    return f"{detail}; " + _pair_provenance(
        buffer_key=OUTPUTD_DAC_BUFFER_KEY,
        buffer_frames=dac_buffer_frames,
        buffer_layer=dac_layer,
        period_frames=period_frames,
        period_layer=period_layer,
        labels=labels,
        override_entries=entries,
        override_label=override_label,
    )


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
    """The identity live fan-in must be running for this route to be real."""

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
    """Stable short fingerprint of everything this plan resolved.

    Published on ``/state`` and by doctor so a config change is visible as a
    changed hash; nothing grades against it.
    """

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


#: Stable reason tokens for the route-policy refusals, published beside their
#: prose as :attr:`AudioRuntimePlan.route_policy_reason_codes`.
ROUTE_POLICY_TRANSPORT_INCOHERENT = "transport_incoherent"
ROUTE_POLICY_FANIN_OFF_RING = "fanin_off_the_ring_pair"
ROUTE_POLICY_BONDED_MEMBER = "bonded_member_has_no_central_ring"
ROUTE_POLICY_OUTPUTD_OFF_RING = "outputd_off_the_ring_pair"


def _route_policy_errors(
    *,
    route: AudioRouteProfile,
    coupling: str | None,
    outputd_env: Mapping[str, str],
    camilla_devices: Mapping[str, Any] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Route-policy refusals as ``(reason_code, message)`` pairs."""
    # Function-local: jasper.transport_coherence imports the transport shape
    # constants and TransportTopology from this module at import time, so this
    # module may only reach back into it from inside a call.
    from jasper.transport_coherence import transport_coherence_errors

    errors = [
        (ROUTE_POLICY_TRANSPORT_INCOHERENT, message)
        for message in transport_coherence_errors(
            coupling=coupling,
            outputd_env=outputd_env,
            camilla_devices=camilla_devices,
        )
    ]
    if route.route_id != ROUTE_USB_LOW_LATENCY_48K:
        return tuple(errors)

    # BOTH HALVES ASK THEIR OWN DAEMON'S ACCEPT SET, not a normalizer. Each
    # daemon serves the ring for an UNDECLARED key and parks on anything it
    # cannot serve, so "the box is on the ring pair" is the question both
    # predicates answer — and a policy that demanded written tokens would
    # turn the shipped low-latency claim red on every box the reconciler has not
    # written yet, which is the mirror of the gap that made the old policy
    # accept the retired pair.
    #
    # `coupling_value_removed` is fan-in's rule inverted: unset / empty /
    # `shm_ring` are served, and everything else — a persisted `loopback` above
    # all — is a config-class fault that exits 78
    # (`rust/jasper-fanin/src/config.rs`). `outputd_content_is_central_ring` is
    # the same question for the post-DSP hop, and it reads the dac-content
    # marker as well as the bridge: a bonded member IS served, but off the ring
    # PAIR this route's latency was measured on, so the claim must not stand.
    fanin_on_ring = not coupling_value_removed(coupling)
    outputd_on_ring = outputd_content_is_central_ring(outputd_env)

    # usb_low_latency_48k runs on the CENTRAL shm_ring pair (Ring A plus
    # whichever central post-DSP ring the box armed). That pair is what was
    # measured, so the route policy accepts it and refuses everything else —
    # including a bonded member, whose post-DSP hop is the bond's return ring.
    if fanin_on_ring and outputd_on_ring:
        return tuple(errors)

    if not fanin_on_ring:
        errors.append((
            ROUTE_POLICY_FANIN_OFF_RING,
            f"{ROUTE_USB_LOW_LATENCY_48K} requires a coherent shm_ring pair; "
            f"{COUPLING_ENV_VAR}={str(coupling or '').strip().lower()} names a "
            "transport jasper-fanin cannot serve, so it is not coherent for "
            "the production low-latency claim",
        ))
    if not outputd_on_ring and dac_content_lane_marker_armed(outputd_env):
        # A BONDED MEMBER, not a misconfigured bridge. Its content comes off the
        # bond's return ring by design, so the central-ring pair this route's
        # latency was measured on does not exist here — and telling the operator
        # to set a bridge key would be telling them to park the daemon.
        errors.append((
            ROUTE_POLICY_BONDED_MEMBER,
            f"{ROUTE_USB_LOW_LATENCY_48K} is not available while this speaker "
            "plays a bond off the dac-content return ring: the route's measured "
            "pair is Ring A plus a CENTRAL post-DSP ring, and a bonded member "
            "attaches no central ring. Ungroup this speaker to claim it again",
        ))
    elif not outputd_on_ring:
        # No `(unset)` fallback on either half: both predicates answer True for
        # an absent or blank value, so a refusal here always has a literal to
        # name.
        raw_bridge = str(
            outputd_env.get(OUTPUTD_CONTENT_BRIDGE_KEY) or ""
        ).strip().lower()
        errors.append((
            ROUTE_POLICY_OUTPUTD_OFF_RING,
            f"{ROUTE_USB_LOW_LATENCY_48K} requires "
            f"{OUTPUTD_CONTENT_BRIDGE_KEY}={OUTPUTD_CONTENT_BRIDGE_SHM_RING}; "
            f"{raw_bridge} is not the one transport",
        ))
    return tuple(errors)


def outputd_grouping_env_file() -> str:
    """``jasper-outputd``'s SECOND ``EnvironmentFile=`` — the path its writer owns.

    Lazy so this module keeps no top-level ``jasper.multiroom`` import (that
    package imports this one).
    """
    from jasper.multiroom.reconcile import OUTPUTD_GROUPING_ENV_FILE

    return OUTPUTD_GROUPING_ENV_FILE


def resolve_outputd_period_setting(
    *,
    base_env: Mapping[str, str],
    override_env: Mapping[str, str],
    generated_env: Mapping[str, str],
    base_label: str,
    override_label: str,
    generated_label: str,
    profile_id: str,
) -> RuntimeSetting:
    """THE outputd period derivation: operator env, lab override, DAC floor.

    One derivation with two callers — :func:`build_audio_runtime_plan`'s
    settings list and the lab-override/floor explain paths — so a consumer that
    only needs the plan's INTENDED period cannot answer it differently. What
    outputd will actually load is :func:`outputd_period_frames_as_loaded`.
    """
    return _resolve_profile_floor_int(
        key=OUTPUTD_PERIOD_KEY,
        default=DEFAULT_OUTPUTD_PERIOD_FRAMES,
        floor_value=getattr(
            latency_floor_for(profile_id) if profile_id else None,
            "outputd_period_frames",
            None,
        ),
        base_env=base_env,
        override_env=override_env,
        generated_env=generated_env,
        base_label=base_label,
        override_label=override_label,
        generated_label=generated_label,
        profile_id=profile_id,
    )


def outputd_period_frames_as_loaded(
    env: "Mapping[str, str] | None" = None,
) -> int | None:
    """The period jasper-outputd WILL LOAD, not the one policy intends.

    The plan's resolver answers a POLICY question — lab override, then
    ``jasper.env``, then the DAC floor, then the packaged default, with
    ``outputd.env`` feeding only warnings. outputd asks a different one:
    ``env_u32("JASPER_OUTPUTD_PERIOD_FRAMES", DEFAULT)`` over its three
    ``EnvironmentFile=`` layers, later wins. The two disagree exactly where the
    slot gate must not guess — a floor of 128 with a stale 1024 still in
    ``outputd.env`` (the resolver's own "rerun audio hardware reconcile" drift),
    or an operator ``jasper.env`` value the reconciler has not applied — so the
    gate reads THIS and the plan keeps its own.

    Blank or absent is the packaged default, matching ``env_parse``. A value
    outputd would REFUSE (non-numeric, or not positive) answers ``None``: the
    daemon would not start at all, so nothing may be armed on it.
    """
    if env is None:
        from jasper.env_load import outputd_reconciled_env

        env = outputd_reconciled_env()
    raw = str(env.get(OUTPUTD_PERIOD_KEY) or "").strip()
    if not raw:
        return DEFAULT_OUTPUTD_PERIOD_FRAMES
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def build_audio_runtime_plan_from_system(
    *,
    base_env_path: str = DEFAULT_BASE_ENV_PATH,
    outputd_env_path: str = DEFAULT_OUTPUTD_ENV_PATH,
    outputd_grouping_env_path: str | None = None,
    fanin_env_path: str = DEFAULT_FANIN_ENV_PATH,
    grouping_env_path: str = DEFAULT_GROUPING_ENV_PATH,
    overrides_path: str | None = None,
    output_hardware_state_path: str | None = None,
) -> AudioRuntimePlan:
    """Build the plan from the same persistent files the daemons load."""

    if outputd_grouping_env_path is None:
        outputd_grouping_env_path = outputd_grouping_env_file()

    base = read_env_file_state(base_env_path)
    outputd = read_env_file_state(outputd_env_path)
    fanin = read_env_file_state(fanin_env_path)
    # The SECOND outputd env layer, read once and handed over separately: the
    # markers live there and the settings never do, so the plan can resolve
    # provenance against `outputd.env` alone while still seeing what outputd
    # started with.
    grouping_outputd = read_env_file_state(outputd_grouping_env_path)
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
    # Lazy: this module is imported at module level by
    # jasper.multiroom.active_leader_config, so a top-level multiroom import
    # here would be a cycle.
    route_mode: RouteMode = "unknown"
    try:
        from jasper.multiroom.config import load_config

        route_mode = route_mode_from_grouping_config(load_config(grouping_env_path))
    except ImportError:
        route_mode = "unknown"
    # The statefile's own reader, not a second one: this plan and the doctor's
    # `current correction` check must never disagree about which config is
    # loaded. Lazy like the other collaborators above — module level would make
    # a widely-imported plan module pull the active-speaker tree.
    from jasper.active_speaker.environment import read_camilla_statefile_config_path

    correction_config_path = read_camilla_statefile_config_path()
    return build_audio_runtime_plan(
        base_env=base_values,
        outputd_env=outputd.values,
        grouping_outputd_env=grouping_outputd.values,
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
    # The SECOND outputd env layer (`grouping-outputd.env`). Kept separate from
    # ``outputd_env`` rather than pre-merged so a provenance label can never
    # name the wrong file: the settings below resolve from ``outputd_env``
    # alone, which is the only layer that carries them.
    grouping_outputd_env: Mapping[str, str] | None = None,
    base_env_label: str = DEFAULT_BASE_ENV_PATH,
    outputd_env_label: str = DEFAULT_OUTPUTD_ENV_PATH,
    fanin_env_label: str = DEFAULT_FANIN_ENV_PATH,
    override_label: str = DEFAULT_AUDIO_RUNTIME_OVERRIDES_PATH,
    plan_warnings: tuple[str, ...] = (),
    correction_config_path: str | None = None,
) -> AudioRuntimePlan:
    """Resolve audio knobs from operator env, generated env, profile, defaults."""

    from jasper.transport_coherence import transport_topology_for_coupling

    base_values = dict(base_env or {})
    outputd_values = dict(outputd_env or {})
    fanin_values = dict(fanin_env or {})
    override_values = dict(overrides or {})
    # What jasper-outputd actually starts with: its two EnvironmentFile= layers
    # in the unit's own order. The MARKERS live in the second layer, so every
    # question about the running transport asks this; the SETTINGS live only in
    # the first, so they keep asking `outputd_values` and their provenance
    # labels stay true.
    outputd_layered = {**outputd_values, **dict(grouping_outputd_env or {})}
    profile_id = (profile_id or "").strip()
    profile = dac_profile_by_id(profile_id) if profile_id else None
    floor = latency_floor_for(profile_id) if profile_id else None
    camilla_floor = camilla_floor_for(profile_id) if profile_id else None
    route_profile = resolve_audio_route_profile(base_values)

    camilla_chunksize_setting = _resolve_profile_floor_int(
        key="JASPER_CAMILLA_CHUNKSIZE",
        default=DEFAULT_CHUNKSIZE,
        floor_value=camilla_floor.chunksize if camilla_floor else None,
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
    camilla_target_setting = _resolve_profile_floor_int(
        key="JASPER_CAMILLA_TARGET_LEVEL",
        default=DEFAULT_TARGET_LEVEL,
        floor_value=camilla_floor.target_level if camilla_floor else None,
        base_env=base_values,
        override_env=override_values,
        generated_env=outputd_values,
        base_label=base_env_label,
        override_label=override_label,
        generated_label=outputd_env_label,
        profile_id=profile_id,
    )
    outputd_period_setting = resolve_outputd_period_setting(
        base_env=base_values,
        override_env=override_values,
        generated_env=outputd_values,
        base_label=base_env_label,
        override_label=override_label,
        generated_label=outputd_env_label,
        profile_id=profile_id,
    )
    settings = [
        camilla_chunksize_setting,
        camilla_target_setting,
        outputd_period_setting,
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
    camilla_devices = read_camilla_devices_config(correction_config_path)
    topology = transport_topology_for_coupling(
        str(coupling_setting.value),
        fanin_env=fanin_values,
        outputd_env=outputd_layered,
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
    route_policy = _route_policy_errors(
        route=route_profile,
        # The RAW declaration, not `coupling_setting.value`: the resolver
        # answers `loopback` for an absent key AND for a persisted one, and the
        # route policy has to tell those apart — one is a box fan-in serves, the
        # other is a box fan-in parks. `_resolve_coupling` already recorded both
        # layers it read.
        coupling=(
            coupling_setting.generated_value
            if coupling_setting.generated_value is not None
            else coupling_setting.operator_value
        ),
        outputd_env=outputd_layered,
        camilla_devices=camilla_devices,
    )
    return AudioRuntimePlan(
        profile_id=profile_id or "unknown",
        profile_label=profile.label if profile is not None else "unknown",
        route_mode=route_mode if route_mode in _VALID_ROUTE_MODES else "unknown",
        settings=tuple(settings),
        transport_topology=topology,
        route_profile=route_profile,
        route_config_hash=route_hash,
        camilla_config_hash=camilla_config_hash,
        correction_latency_eligibility=correction_latency,
        outputd_env=outputd_layered,
        route_policy_errors=tuple(m for _c, m in route_policy),
        route_policy_reason_codes=tuple(c for c, _m in route_policy),
        plan_warnings=combined_plan_warnings,
        camilla_emitted=_emitted_camilla_geometry(
            correction_config_path, camilla_devices
        ),
    )


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
    from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE, RING_PLAYBACK_DEVICE

    fallback: dict[str, Any] | None = None
    errors: list[str] = []
    # EVERY device that can be a post-DSP output endpoint. An endpoint absent
    # from this set is not merely unrecognized: the evidence degrades to
    # ``endpoint_recognized=False`` and every consumer downgrades to "coherence
    # unknown", so a healthy box on a new endpoint would look unverifiable
    # forever rather than fail loudly once.
    output_endpoints = {
        DEFAULT_PLAYBACK_DEVICE,
        ACTIVE_OUTPUTD_PLAYBACK_DEVICE,
        RING_PLAYBACK_DEVICE,
        RING_ACTIVE_PLAYBACK_DEVICE,
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
        elif setting.source_kind == "device_profile":
            actions.append(RuntimeEnvAction("set", key, str(setting.value)))
        else:
            actions.append(RuntimeEnvAction("unset", key))
    return tuple(actions)


def apply_capture_precedence(
    emit_kwargs: Mapping[str, object],
    fanin_coupling_capture_kwargs: Mapping[str, object] | None,
    *,
    member_kwargs: Mapping[str, object] | None,
) -> EmitSoundConfigKwargs:
    """Apply capture-precedence policy to an ``emit_sound_config`` kwargs dict.

    The coupling is END-TO-END, but only its PLAYBACK half is ever owned by a
    more-specific topology: a member's ``playback_pipe_path`` owns the sink, so
    that emit takes :func:`~jasper.fanin_coupling.capture_half` only. It must
    still take THAT — dropping the capture half too re-emits a bonded leader's
    LIVE camilla#1 onto the tap an armed ring took fan-in off, silencing the
    whole bond. Everything else takes both halves. Empty coupling kwargs return
    the input unchanged (detached, for callers to mutate).
    """

    if not fanin_coupling_capture_kwargs:
        return cast(EmitSoundConfigKwargs, dict(emit_kwargs))
    merged = dict(emit_kwargs)
    if (member_kwargs or {}).get("playback_pipe_path"):
        merged.update(capture_half(fanin_coupling_capture_kwargs))
    else:
        merged.update(fanin_coupling_capture_kwargs)
    return cast(EmitSoundConfigKwargs, merged)


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
) -> tuple[int, str | None, int | None]:
    """Resolve one integer knob across the env layers, highest-precedence first.

    Third element is the INDEX into ``layers`` that supplied the value, or
    ``None`` when no layer stated it and the packaged default applies. The
    caller needs that to tell an operator which file to edit — the value alone
    cannot, and a refusal that names only the key is what left #2489 pointing
    at the wrong file.
    """
    for index in reversed(range(len(layers))):
        raw = _raw(layers[index], key)
        if raw is None:
            continue
        value, error = _positive_int(raw)
        if error is not None or value is None:
            return default, f"{key}={raw!r} is invalid ({error})", index
        return value, None, index
    return default, None, None


@dataclass(frozen=True)
class _PositiveIntPolicy:
    """Policy-specific provenance and warning vocabulary for one integer knob."""

    value: int | None
    source_kind: SourceKind
    source: str
    name: str
    owner_id: str
    absent_detail: str
    override_scope: str
    packaged_default: int
    packaged_source: str


def _resolve_layered_policy_int(
    *,
    key: str,
    policy: _PositiveIntPolicy,
    base_env: Mapping[str, str],
    override_env: Mapping[str, str],
    generated_env: Mapping[str, str],
    base_label: str,
    override_label: str,
    generated_label: str,
) -> RuntimeSetting:
    """Resolve override/operator/policy/default precedence for a positive int."""

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
    if override_raw is not None and (
        operator_raw is not None or generated_raw is not None
    ):
        warnings.append(
            f"{key} lab override in {override_label} is active; it intentionally "
            f"wins over env/{policy.override_scope} values"
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

    if policy.value is not None:
        if generated_value is None:
            warnings.append(
                f"{key} {policy.name} for {policy.owner_id} is {policy.value}, but "
                f"{generated_label} does not emit it; run "
                "jasper-audio-hardware-reconcile"
            )
        elif generated_value != policy.value:
            warnings.append(
                f"{key} in {generated_label} is {generated_value}, but the "
                f"{policy.owner_id} {policy.name} is {policy.value}; rerun "
                "audio hardware reconcile"
            )
        return RuntimeSetting(
            key=key,
            value=policy.value,
            source_kind=policy.source_kind,
            source=policy.source,
            unit="frames",
            operator_value=operator_raw,
            generated_value=generated_raw,
            warnings=tuple(warnings),
        )

    if generated_value is not None and generated_value != policy.packaged_default:
        warnings.append(
            f"{key} in {generated_label} is {generated_value}, but the active "
            f"{policy.absent_detail}; stale generated value will override the "
            f"packaged default {policy.packaged_default}"
        )
    return RuntimeSetting(
        key=key,
        value=policy.packaged_default,
        source_kind="packaged_default",
        source=policy.packaged_source,
        unit="frames",
        operator_value=operator_raw,
        generated_value=generated_raw,
        warnings=tuple(warnings),
    )


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
    return _resolve_layered_policy_int(
        key=key,
        policy=_PositiveIntPolicy(
            value=floor_value,
            source_kind="device_profile",
            source=f"DacProfile:{profile_id}",
            name="profile floor",
            owner_id=profile_id,
            absent_detail="profile has no floor",
            override_scope="profile",
            packaged_default=default,
            packaged_source="packaged systemd/Camilla default",
        ),
        base_env=base_env,
        override_env=override_env,
        generated_env=generated_env,
        base_label=base_label,
        override_label=override_label,
        generated_label=generated_label,
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
    if raw is not None and coupling is None and raw.strip():
        warnings.append(
            f"{COUPLING_ENV_VAR}={raw!r} names no transport this box has; "
            "jasper-fanin refuses it and parks (exit 78)"
        )
    return RuntimeSetting(
        key=COUPLING_ENV_VAR,
        # The transport the files NAME, else the token they carry verbatim —
        # never a substituted one, which is what made an unwritten key read as a
        # route this repo deleted.
        value=coupling if coupling is not None else (raw or "").strip().lower(),
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
