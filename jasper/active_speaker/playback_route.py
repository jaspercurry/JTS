# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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

import logging
from dataclasses import dataclass
from typing import Any

from jasper.audio_hardware.dac import by_id as _dac_by_id
from jasper.log_event import log_event
from jasper.output_topology import (
    ACTIVE_PLAYBACK_DEVICE_ENV,
    EXPLICIT_SOURCE,
    MISSING_SOURCE,
    OUTPUTD_ACTIVE_LANE_SOURCE,
    OutputLayout,
    OutputTopology,
    SpeakerGroup,
    resolve_output_layout,
)

from ._common import issue as _issue

logger = logging.getLogger(__name__)

# Re-exported for backwards compatibility — these constants moved to
# jasper.output_topology (the resolution owner) but several active-speaker
# callers import them from here.
__all__ = [
    "ACTIVE_PLAYBACK_DEVICE_ENV",
    "ACTIVE_PLAYBACK_ROUTE_KIND",
    "EXPLICIT_SOURCE",
    "LOADED_GRAPH_SOURCE",
    "MISSING_SOURCE",
    "OUTPUTD_ACTIVE_LANE_SOURCE",
    "ActiveLaneCapabilityGap",
    "ActivePlaybackRouteCapability",
    "active_lane_capability_gap",
    "active_playback_route_capability",
    "resolve_active_playback_device",
    "resolve_live_active_endpoint",
]

ACTIVE_PLAYBACK_ROUTE_KIND = "jts_active_speaker_playback_route_capability"

# The witness that answered :func:`resolve_live_active_endpoint`: the durable
# CamillaDSP graph itself. Its own token rather than a reuse of
# ``OUTPUTD_ACTIVE_LANE_SOURCE`` because the two are different claims — "the
# marker selects this transport" vs "the box's graph IS on this transport" —
# and a test that could not tell them apart would pass on an unarmed box, where
# both witnesses answer the same device, while proving nothing.
LOADED_GRAPH_SOURCE = "loaded_graph"


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


@dataclass(frozen=True)
class ActiveLaneCapabilityGap:
    """A saved layout needs the active outputd lane; this DAC declares none."""

    device_id: str
    device_label: str

    def to_dict(self) -> dict[str, str]:
        return {"device_id": self.device_id, "device_label": self.device_label}


def active_lane_capability_gap(
    topology: OutputTopology,
) -> ActiveLaneCapabilityGap | None:
    """Return why ``topology`` can never reach hardware on this DAC, or None.

    The one predicate for a permanently-undrivable pairing: the layout needs a
    roleful (crossover / protected / subwoofer) graph, but the resolved
    ``DacProfile`` declares no active outputd lane. CamillaDSP then plays the
    roleful graph into the active loopback lane while outputd captures the
    passive one, and the speaker emits digital silence with every daemon
    healthy. Re-running a reconciler cannot fix it — only a different layout
    or different hardware can.

    Every surface that explains this state (``/state`` audio health, doctor's
    remedy, the ``/sound/setup/`` save guard) resolves it here and phrases its
    own sentence; the predicate itself has one owner.

    Strict on purpose: an unrecognized ``device_id`` has no profile to read a
    capability off, so it returns ``None`` rather than guessing a gap that
    would block a save on hardware the registry simply has not met.
    """

    from jasper.active_speaker.runtime_contract import (
        active_topology_requires_roleful_graph,
    )

    if not active_topology_requires_roleful_graph(topology):
        return None
    device_id = topology.hardware.device_id
    profile = _dac_by_id(device_id)
    if profile is None or profile.supports_active_outputd_lane:
        return None
    return ActiveLaneCapabilityGap(
        device_id=device_id,
        # The registry owns the DAC's name; the topology's saved label is
        # detection data that can be stale.
        device_label=profile.label or topology.hardware.device_label or device_id,
    )


def resolve_active_playback_device(
    topology: OutputTopology,
    *,
    playback_device: str | None = None,
) -> tuple[str | None, str]:
    """Return the active-speaker playback PCM."""

    layout = resolve_output_layout(
        topology,
        playback_device=playback_device,
    )
    return layout.playback_device, layout.playback_device_source


def resolve_live_active_endpoint(
    topology: OutputTopology,
) -> tuple[str | None, str]:
    """The playback endpoint this box's active graph is CURRENTLY on.

    ONE derivation, for every seam that RE-EMITS or RE-DERIVES an active graph
    the box is already running. Those seams must not *choose* an endpoint — the
    box is on one, and a re-emit that answers a different one moves the speaker
    without anyone asking. :func:`resolve_active_playback_device` answers the
    other question ("which endpoint should a graph be emitted against?") and
    stays the right call for a FRESH emit.

    THE GRAPH IS UPSTREAM TRUTH, so it is asked first. The endpoint marker
    (``JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT``) is *derived by*
    ``jasper-audio-hardware-reconcile`` from the classification of the graph the
    statefile points at, so in every state where the two disagree the graph is
    the half the reconcilers are converging toward:

    * Mid-arm (``baseline-reemit --endpoint ring`` has run, the hardware
      reconciler has not): graph=ring, marker=clear. Following the marker here
      re-emits the ALSA lane over the rung that was just completed and the
      ladder cannot finish — which is exactly how a deploy landing in this
      window used to de-arm a box.
    * Legacy mid-rollback (graph=aloop, marker=ring) — reachable only as STATE
      LEFT BEHIND, never as a new act: the rollback that produced it was retired
      with the aloop endpoint. Following the graph still refuses the retired
      device rather than adopting it; the way out is forward, by re-arming.
    * Marker set, graph moved back by something else: following the graph
      converges with the next hardware reconcile, which will clear the marker
      from that same graph.

    ONLY THE ACTIVE LANE'S OWN TRANSPORT is adopted from the graph
    (:data:`~jasper.active_speaker.runtime_contract.OUTPUTD_LEGAL_ENDPOINT_DEVICES`,
    a ONE-member set since #2285 P2 retired the snd-aloop ACTIVE endpoint),
    because that is the whole question this function exists to answer. The set
    is still read as a membership rather than an equality: it is the one place
    that decides what is legal, and the legacy-mid-rollback bullet above is
    exactly a graph naming something outside it. Anything else the graph might
    name — a stale stereo lane left in the
    statefile, a lab PCM, a grouped pipe sink — is not a third answer to
    "which transport", and adopting it would hand the active emitter a device
    its own forbidden-token guard then refuses mid-save. Those fall through to
    the chooser below, which already honours an explicit
    ``JASPER_ACTIVE_SPEAKER_PLAYBACK_DEVICE`` override, so a lab box reaches the
    same device by the route that owns lab overrides.

    The MARKER is the second witness, not a peer: it answers only when the graph
    does not. A fresh box has no statefile at all and must still deploy, so this
    is DEFAULT-SAFE rather than fail-loud — and it is never worse than the
    alternative it replaced, because an armed box with an unreadable statefile
    still reads its ring endpoint off the marker where the applied snapshot
    would have named the ALSA lane.

    ``(None, MISSING_SOURCE)`` — a topology that resolves no active endpoint at
    all — is passed through rather than invented over, so a caller threading
    this into ``recompose_applied_baseline_yaml(playback_device=...)`` lands on
    that function's own snapshot default and stays byte-identical to before.

    COST: one statefile read plus one config read, fresh, per call — nothing is
    cached, deliberately, because a re-emit that acted on a stale endpoint is the
    defect this exists to prevent. That is right for the four cold seams that
    call it (a deploy reconcile, a wizard save, a bass-extension apply, and the
    commissioning wizard's applied-summed measurement load); a future
    caller on a warm path should know it is paying two file reads each time and
    snapshot the answer itself rather than making this one lie.
    """

    # Lazy: coupling_reconcile is the arm/disarm transition module and pulls in
    # the whole fan-in env layer; this reader is one function on it and every
    # caller here is a cold path. runtime_contract is the owner of which devices
    # are legal active endpoints — read, never restated.
    from jasper.active_speaker.runtime_contract import OUTPUTD_LEGAL_ENDPOINT_DEVICES
    from jasper.fanin.coupling_reconcile import read_loaded_camilla_graph

    graph = read_loaded_camilla_graph()
    device = graph.devices.get("playback_device")
    if isinstance(device, str) and device.strip():
        named = device.strip()
        if named in OUTPUTD_LEGAL_ENDPOINT_DEVICES:
            return named, LOADED_GRAPH_SOURCE
        # The graph names a sink that is not one of the active lane's two
        # transports — a stale stereo lane, a lab PCM, a pipe. Declining it is
        # correct (see above), but the moment of observation should be visible:
        # the doctor's coupling check will report the incoherence later, and a
        # journal line here is what says the endpoint derivation SAW it and
        # deferred to the chooser rather than never having looked. DEBUG, not
        # WARNING: a lab box is in this branch legitimately on every call.
        log_event(
            logger,
            "active_speaker.live_endpoint",
            level=logging.DEBUG,
            result="declined_non_endpoint_device",
            observed=named,
            config=graph.path or "",
            answered_by="playback_route_chooser",
        )
    return resolve_active_playback_device(topology)


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


def active_playback_route_capability(
    topology: OutputTopology,
    *,
    playback_device: str | None = None,
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
