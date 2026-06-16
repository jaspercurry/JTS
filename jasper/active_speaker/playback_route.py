"""Active-speaker playback route capability contract.

The output topology can describe every physical DAC lane, but active-speaker
test/apply audio reaches hardware through a narrower runtime route. This module
keeps that distinction explicit so UI and DSP builders do not infer capability
from physical output count alone.

Route resolution itself (the stable ``hw:CARD=`` identity, the DAC-agnostic
transport plan) lives on :mod:`jasper.output_topology` — the runtime owner that
already reads env and the topology's card identity. This module is a thin
active-speaker reader over :func:`~jasper.output_topology.resolve_output_layout`:
it adds the speaker-group demand accounting and the route-fit issues.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jasper.output_topology import (
    ACTIVE_PLAYBACK_DEVICE_ENV,
    DIRECT_DAC_SOURCE,
    EXPLICIT_SOURCE,
    MISSING_SOURCE,
    OUTPUTD_ACTIVE_LANE_SOURCE,
    OutputLayout,
    OutputTopology,
    SpeakerGroup,
    resolve_output_layout,
)

from ._common import issue as _issue

# Re-exported for backwards compatibility — these constants moved to
# jasper.output_topology (the resolution owner) but several active-speaker
# callers import them from here.
__all__ = [
    "ACTIVE_PLAYBACK_DEVICE_ENV",
    "ACTIVE_PLAYBACK_ROUTE_KIND",
    "DIRECT_DAC_SOURCE",
    "EXPLICIT_SOURCE",
    "MISSING_SOURCE",
    "OUTPUTD_ACTIVE_LANE_SOURCE",
    "ActivePlaybackRouteCapability",
    "active_playback_route_capability",
    "durable_profile_route_capability",
    "resolve_active_playback_device",
    "resolve_diagnostic_playback_device",
    "resolve_durable_profile_playback_device",
]

ACTIVE_PLAYBACK_ROUTE_KIND = "jts_active_speaker_playback_route_capability"


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


def resolve_durable_profile_playback_device(
    topology: OutputTopology,
    *,
    playback_device: str | None = None,
) -> tuple[str | None, str]:
    """Return the playback PCM for durable active-speaker profiles.

    Durable profile apply must hand audio to an outputd-owned active lane. A
    temporary direct-DAC route can be safe enough for one short diagnostic tone,
    but it must not become the speaker's normal output path, so this never falls
    back to a direct-DAC route.
    """

    layout = resolve_output_layout(
        topology,
        playback_device=playback_device,
        allow_direct_dac=False,
    )
    return layout.playback_device, layout.playback_device_source


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

    layout = resolve_output_layout(
        topology,
        playback_device=playback_device,
        allow_direct_dac=True,
    )
    return layout.playback_device, layout.playback_device_source


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

    Thin reader: the route half (resolved device, source, transport width,
    subwoofer support) comes from the resolved ``OutputLayout``; this function
    adds the speaker-group demand accounting and the route-fit issues. Physical
    DAC output count is not enough — the active-speaker transport width is
    profile-declared because it can differ from the DAC's analog output count.
    """

    layout: OutputLayout = resolve_output_layout(
        topology,
        playback_device=playback_device,
        allow_direct_dac=diagnostic,
    )
    active_groups = _active_main_groups(topology)
    subwoofer_groups = _subwoofer_groups(topology)
    required_groups = active_groups + subwoofer_groups
    highest = _highest_assigned_output(required_groups)
    required_outputs = (highest + 1) if highest is not None else 0

    resolved_device = layout.playback_device
    transport_channels = layout.transport_channel_count

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
        playback_device_source=layout.playback_device_source,
        transport_channel_count=transport_channels,
        required_active_output_count=required_outputs,
        active_group_count=len(active_groups),
        subwoofer_group_count=len(subwoofer_groups),
        subwoofer_supported=layout.subwoofer_supported,
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
