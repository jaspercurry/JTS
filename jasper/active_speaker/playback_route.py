"""Active-speaker playback route capability contract.

The output topology can describe every physical DAC lane, but active-speaker
test/apply audio reaches hardware through a narrower runtime route. This module
keeps that distinction explicit so UI and DSP builders do not infer capability
from physical output count alone.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from jasper.audio_hardware import dac
from jasper.camilla_config_contract import ACTIVE_OUTPUTD_PLAYBACK_DEVICE
from jasper.output_topology import OutputTopology, SpeakerGroup
from ._common import issue as _issue

ACTIVE_PLAYBACK_DEVICE_ENV = "JASPER_ACTIVE_SPEAKER_PLAYBACK_DEVICE"
ACTIVE_PLAYBACK_ROUTE_KIND = "jts_active_speaker_playback_route_capability"
OUTPUTD_ACTIVE_LANE_SOURCE = "outputd_active_lane"
EXPLICIT_SOURCE = "explicit"
DIRECT_DAC_SOURCE = "topology_direct_dac"
MISSING_SOURCE = "missing"


def _active_main_groups(topology: OutputTopology) -> list[SpeakerGroup]:
    return [
        group
        for group in topology.speaker_groups
        if group.mode in {"active_2_way", "active_3_way"}
        and group.kind != "subwoofer"
    ]


def _subwoofer_groups(topology: OutputTopology) -> list[SpeakerGroup]:
    routed = set(topology.routing.subwoofer_group_ids)
    return [
        group
        for group in topology.speaker_groups
        if (
            group.kind == "subwoofer"
            or group.mode == "subwoofer"
            or group.id in routed
        )
    ]


def _highest_assigned_output(groups: list[SpeakerGroup]) -> int | None:
    indexes = [
        channel.physical_output_index
        for group in groups
        for channel in group.channels
        if channel.physical_output_index is not None
    ]
    return max(indexes) if indexes else None


def _direct_dac_pcm(card_id: str | None) -> str | None:
    card = (card_id or "").strip()
    if not card:
        return None
    return f"hw:CARD={card},DEV=0"


def _profile_outputd_lane_device(topology: OutputTopology) -> tuple[str | None, str]:
    profile = dac.by_id(topology.hardware.device_id)
    if (
        profile is not None
        and profile.supports_active_outputd_lane
        and profile.active_outputd_lane_channels
    ):
        return ACTIVE_OUTPUTD_PLAYBACK_DEVICE, OUTPUTD_ACTIVE_LANE_SOURCE
    return None, MISSING_SOURCE


def resolve_durable_profile_playback_device(
    topology: OutputTopology,
    *,
    playback_device: str | None = None,
) -> tuple[str | None, str]:
    """Return the playback PCM for durable active-speaker profiles.

    Durable profile apply must hand audio to an outputd-owned active lane. A
    temporary direct-DAC route can be safe enough for one short diagnostic tone,
    but it must not become the speaker's normal output path.
    """

    explicit = playback_device or os.environ.get(ACTIVE_PLAYBACK_DEVICE_ENV)
    if explicit and explicit.strip():
        return explicit.strip(), EXPLICIT_SOURCE
    return _profile_outputd_lane_device(topology)


def resolve_diagnostic_playback_device(
    topology: OutputTopology,
    *,
    playback_device: str | None = None,
) -> tuple[str | None, str]:
    """Return the playback PCM for bounded driver-test diagnostics.

    Diagnostic tones may temporarily use a coherent single-DAC hardware route
    when no outputd active lane exists. Callers that compile, stage, or apply
    normal speaker profiles must use ``resolve_durable_profile_playback_device``
    instead.
    """

    explicit = playback_device or os.environ.get(ACTIVE_PLAYBACK_DEVICE_ENV)
    if explicit and explicit.strip():
        return explicit.strip(), EXPLICIT_SOURCE
    outputd_device, outputd_source = _profile_outputd_lane_device(topology)
    if outputd_device:
        return outputd_device, outputd_source
    profile = dac.by_id(topology.hardware.device_id)
    if (
        profile is not None
        and profile.kind == "single"
        and profile.coherent_clock_domain
        and topology.hardware.card_id
    ):
        return _direct_dac_pcm(topology.hardware.card_id), DIRECT_DAC_SOURCE
    if topology.hardware.card_id:
        return _direct_dac_pcm(topology.hardware.card_id), DIRECT_DAC_SOURCE
    return None, MISSING_SOURCE


def resolve_active_playback_device(
    topology: OutputTopology,
    *,
    playback_device: str | None = None,
) -> tuple[str | None, str]:
    """Return the diagnostic playback PCM for active-speaker tests.

    Kept as the compatibility name for existing test/readiness callers. New
    durable profile/staging code should call
    ``resolve_durable_profile_playback_device`` explicitly.
    """

    return resolve_diagnostic_playback_device(
        topology,
        playback_device=playback_device,
    )


@dataclass(frozen=True)
class ActivePlaybackRouteCapability:
    """Current active-speaker runtime route capacity for a saved topology."""

    playback_device: str | None
    playback_device_source: str
    transport_channel_count: int
    required_active_output_count: int
    active_group_count: int
    subwoofer_group_count: int
    subwoofer_supported: bool
    issues: tuple[dict[str, str], ...]

    @property
    def fits_required_outputs(self) -> bool:
        return (
            self.required_active_output_count <= self.transport_channel_count
            if self.transport_channel_count > 0
            else self.required_active_output_count == 0
        )

    @property
    def ready(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": ACTIVE_PLAYBACK_ROUTE_KIND,
            "playback_device": self.playback_device,
            "playback_device_source": self.playback_device_source,
            "transport_channel_count": self.transport_channel_count,
            "required_active_output_count": self.required_active_output_count,
            "active_group_count": self.active_group_count,
            "subwoofer_group_count": self.subwoofer_group_count,
            "subwoofer_supported": self.subwoofer_supported,
            "fits_required_outputs": self.fits_required_outputs,
            "ready": self.ready,
            "issues": list(self.issues),
        }


def _route_capability(
    topology: OutputTopology,
    *,
    playback_device: str | None = None,
    diagnostic: bool,
) -> ActivePlaybackRouteCapability:
    """Return the active-speaker runtime route capacity.

    Physical DAC output count is not enough: the active-speaker outputd
    transport width is profile-declared because it can differ from the DAC's
    analog output count.
    """

    resolver = (
        resolve_diagnostic_playback_device
        if diagnostic else resolve_durable_profile_playback_device
    )
    resolved_device, source = resolver(topology, playback_device=playback_device)
    active_groups = _active_main_groups(topology)
    subwoofer_groups = _subwoofer_groups(topology)
    required_groups = active_groups + subwoofer_groups
    highest = _highest_assigned_output(required_groups)
    required_outputs = (highest + 1) if highest is not None else 0
    if source == OUTPUTD_ACTIVE_LANE_SOURCE:
        transport_channels = (
            dac.active_outputd_lane_channels_for(topology.hardware.device_id) or 0
        )
        subwoofer_supported = True
    elif source == EXPLICIT_SOURCE or (diagnostic and source == DIRECT_DAC_SOURCE):
        # Explicit lab routes and coherent single-DAC direct routes use the
        # saved hardware width as the upper bound. The user-facing topology
        # decides which of those physical outputs are actually assigned.
        transport_channels = max(0, int(topology.hardware.physical_output_count or 0))
        subwoofer_supported = True
    else:
        transport_channels = 0
        subwoofer_supported = False

    issues: list[dict[str, str]] = []
    if active_groups and not resolved_device:
        issues.append(_issue(
            "blocker",
            "active_playback_route_unavailable",
            "active-speaker tests need a resolved playback route",
        ))
    if active_groups and resolved_device and transport_channels <= 0:
        issues.append(_issue(
            "blocker",
            "active_playback_route_width_unknown",
            "active-speaker tests need a profile-declared playback route width",
        ))
    if transport_channels and required_outputs > transport_channels:
        issues.append(_issue(
            "blocker",
            "active_playback_route_too_narrow",
            (
                f"This install can drive {transport_channels} active output "
                f"lanes, but this layout uses DAC output {required_outputs}."
            ),
        ))

    return ActivePlaybackRouteCapability(
        playback_device=resolved_device,
        playback_device_source=source,
        transport_channel_count=transport_channels,
        required_active_output_count=required_outputs,
        active_group_count=len(active_groups),
        subwoofer_group_count=len(subwoofer_groups),
        subwoofer_supported=subwoofer_supported,
        issues=tuple(issues),
    )


def active_playback_route_capability(
    topology: OutputTopology,
    *,
    playback_device: str | None = None,
) -> ActivePlaybackRouteCapability:
    """Return the route capacity for bounded diagnostic test playback."""

    return _route_capability(
        topology,
        playback_device=playback_device,
        diagnostic=True,
    )


def durable_profile_route_capability(
    topology: OutputTopology,
    *,
    playback_device: str | None = None,
) -> ActivePlaybackRouteCapability:
    """Return the route capacity for durable active profile apply/staging."""

    return _route_capability(
        topology,
        playback_device=playback_device,
        diagnostic=False,
    )
