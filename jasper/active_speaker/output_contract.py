# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Runtime output contract of the saved speaker topology.

``jasper.output_topology`` owns the declarative physical-output contract; this
leaf classifies it and derives the flat-DAC and roleful predicates and the ring
widths, plus the emitted-graph source names the verifier recognises. It reads
no CamillaDSP graph (graph legality is
:mod:`jasper.active_speaker.runtime_contract`'s question), so a caller that
only classifies does not import the graph proofs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from jasper.output_topology import (
    LOWEST_DRIVER_ROLE_BY_MAIN_MODE,
    OutputTopology,
    SpeakerChannel,
    SpeakerGroup,
)

from jasper.ring_header import MAX_RING_CHANNELS, MIN_RING_CHANNELS

from ._common import issue as _issue


ACTIVE_BASELINE_SOURCE = (
    "jasper.active_speaker.camilla_yaml.emit_active_speaker_baseline_config"
)
# The follower's driver-domain-only (Layer-A) emit. Independently named here
# (not imported from the emitter) so the verifier re-proves the graph without
# trusting the producer — emitter<->verifier independence, exactly as
# ACTIVE_BASELINE_SOURCE is. The keystone round-trip test pins that the two
# spellings match.
ACTIVE_DRIVER_DOMAIN_SOURCE = (
    "jasper.active_speaker.camilla_yaml.emit_active_speaker_driver_domain_config"
)

# The protected-neutral CHECK/MEASURE emit. Named here only to REFUSE it by its
# own name: it is neither baseline-shaped nor a commissioning bring-up graph. It
# has no proof arm in the runtime graph door; its protection is proved per
# segment and per output index by program admission instead.
ACTIVE_PROGRAM_SOURCE = (
    "jasper.active_speaker.camilla_yaml.emit_active_speaker_program_config"
)

CONTRACT_UNCONFIGURED = "unconfigured"
CONTRACT_NORMAL_STEREO_FULL_RANGE = "normal_stereo_full_range"
CONTRACT_NORMAL_MONO_FULL_RANGE = "normal_mono_full_range"
CONTRACT_ACTIVE_MONO_2WAY = "active_mono_2way"
CONTRACT_ACTIVE_MONO_3WAY = "active_mono_3way"
CONTRACT_ACTIVE_STEREO_2WAY = "active_stereo_2way"
CONTRACT_ACTIVE_STEREO_3WAY = "active_stereo_3way"
CONTRACT_SUBWOOFER_PRESENT = "subwoofer_present"
CONTRACT_PROTECTED_OUTPUTS_PRESENT = "protected_outputs_present"
CONTRACT_UNKNOWN_OR_INVALID = "unknown_or_invalid"


@dataclass(frozen=True)
class OutputAssignment:
    speaker_group_id: str
    speaker_label: str
    speaker_kind: str
    speaker_mode: str
    role: str
    physical_output_index: int | None
    startup_muted: bool
    protection_required: bool
    output_variant: str = "primary"

    @property
    def roleful(self) -> bool:
        return self.role != "full_range"

    @property
    def protected(self) -> bool:
        return self.role == "tweeter" or self.protection_required

    @property
    def output_label(self) -> str:
        if self.physical_output_index is None:
            return "unassigned DAC output"
        return f"DAC output {self.physical_output_index + 1}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "speaker_group_id": self.speaker_group_id,
            "speaker_label": self.speaker_label,
            "speaker_kind": self.speaker_kind,
            "speaker_mode": self.speaker_mode,
            "role": self.role,
            **({"output_variant": self.output_variant} if self.output_variant != "primary" else {}),
            "physical_output_index": self.physical_output_index,
            "startup_muted": self.startup_muted,
            "protection_required": self.protection_required,
            "roleful": self.roleful,
            "protected": self.protected,
        }


@dataclass(frozen=True)
class OutputContract:
    classification: str
    topology_configured: bool
    main_layout: str
    active_modes: tuple[str, ...] = ()
    subwoofer_present: bool = False
    protected_outputs_present: bool = False
    roleful_outputs_present: bool = False
    requires_roleful_graph: bool = False
    assignments: tuple[OutputAssignment, ...] = ()
    issues: tuple[dict[str, str], ...] = ()

    @property
    def roleful_assignments(self) -> tuple[OutputAssignment, ...]:
        return tuple(item for item in self.assignments if item.roleful)

    @property
    def protected_assignments(self) -> tuple[OutputAssignment, ...]:
        return tuple(item for item in self.assignments if item.protected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "topology_configured": self.topology_configured,
            "main_layout": self.main_layout,
            "active_modes": list(self.active_modes),
            "subwoofer_present": self.subwoofer_present,
            "protected_outputs_present": self.protected_outputs_present,
            "roleful_outputs_present": self.roleful_outputs_present,
            "requires_roleful_graph": self.requires_roleful_graph,
            "assignments": [item.to_dict() for item in self.assignments],
            "issues": list(self.issues),
        }


def _assignments(topology: OutputTopology) -> tuple[OutputAssignment, ...]:
    out: list[OutputAssignment] = []
    for group in topology.speaker_groups:
        for channel in group.channels:
            out.append(_assignment(group, channel))
    return tuple(out)


def _assignment(group: SpeakerGroup, channel: SpeakerChannel) -> OutputAssignment:
    return OutputAssignment(
        speaker_group_id=group.id,
        speaker_label=group.label,
        speaker_kind=group.kind,
        speaker_mode=group.mode,
        role=channel.role,
        output_variant=channel.output_variant,
        physical_output_index=channel.physical_output_index,
        startup_muted=bool(channel.startup_muted),
        protection_required=bool(channel.protection_required),
    )


def _subwoofer_groups(topology: OutputTopology) -> list[SpeakerGroup]:
    routed = set(topology.routing.subwoofer_group_ids)
    return [
        group
        for group in topology.speaker_groups
        if group.kind == "subwoofer" or group.mode == "subwoofer" or group.id in routed
    ]


def _main_layout(groups: Iterable[SpeakerGroup]) -> str:
    kinds = {group.kind for group in groups if group.kind != "subwoofer"}
    if "mono" in kinds:
        return "mono"
    if {"left", "right"} <= kinds:
        return "stereo"
    if not kinds:
        return "unconfigured"
    return "unknown"


def classify_output_contract(topology: OutputTopology) -> OutputContract:
    """Classify the saved output topology as the runtime safety contract."""

    assignments = _assignments(topology)
    roleful = tuple(item for item in assignments if item.roleful)
    protected = tuple(item for item in assignments if item.protected)
    active_groups = tuple(
        group for group in topology.speaker_groups
        if group.mode in {"active_2_way", "active_3_way"} and group.kind != "subwoofer"
    )
    subwoofers = _subwoofer_groups(topology)
    layout = _main_layout(topology.speaker_groups)
    active_modes = tuple(sorted({group.mode for group in active_groups}))
    issues = tuple(
        _issue(
            str(item.get("severity") or "blocker"),
            str(item.get("code") or "topology_issue"),
            str(item.get("message") or item.get("code") or "topology issue"),
        )
        for item in topology.evaluation().get("blockers", [])
        if isinstance(item, dict)
    )

    if not topology.speaker_groups:
        classification = CONTRACT_UNCONFIGURED
    elif subwoofers and not active_groups:
        classification = CONTRACT_SUBWOOFER_PRESENT
    elif not active_groups and protected:
        classification = CONTRACT_PROTECTED_OUTPUTS_PRESENT
    elif not active_groups:
        classification = (
            CONTRACT_NORMAL_STEREO_FULL_RANGE
            if layout == "stereo"
            else CONTRACT_NORMAL_MONO_FULL_RANGE
            if layout == "mono"
            else CONTRACT_UNKNOWN_OR_INVALID
        )
    elif layout == "mono" and active_modes == ("active_2_way",):
        classification = CONTRACT_ACTIVE_MONO_2WAY
    elif layout == "mono" and active_modes == ("active_3_way",):
        classification = CONTRACT_ACTIVE_MONO_3WAY
    elif layout == "stereo" and active_modes == ("active_2_way",):
        classification = CONTRACT_ACTIVE_STEREO_2WAY
    elif layout == "stereo" and active_modes == ("active_3_way",):
        classification = CONTRACT_ACTIVE_STEREO_3WAY
    else:
        classification = CONTRACT_UNKNOWN_OR_INVALID

    # Subwoofers are roleful even without tweeter protection: flat stereo
    # should not be selected as their fallback unless a later runtime contract
    # explicitly teaches JTS how to drive that topology safely.
    requires_roleful_graph = bool(roleful or protected or subwoofers)
    return OutputContract(
        classification=classification,
        topology_configured=bool(topology.speaker_groups),
        main_layout=layout,
        active_modes=active_modes,
        subwoofer_present=bool(subwoofers),
        protected_outputs_present=bool(protected),
        roleful_outputs_present=bool(roleful),
        requires_roleful_graph=requires_roleful_graph,
        assignments=assignments,
        issues=issues,
    )


def topology_allows_flat_dac_graph(contract: OutputContract) -> bool:
    """Whether this explicit topology may send a flat program to a DAC.

    ``requires_roleful_graph`` answers a narrower question: whether a topology
    needs per-driver DSP.  It must not double as permission for full-range DAC
    playback.  In particular, an empty draft has no roleful outputs but has not
    declared any speaker at all.  Flat playback is allowed only after the
    household has explicitly saved one complete passive main layout.
    """

    return (
        contract.classification
        in (CONTRACT_NORMAL_STEREO_FULL_RANGE, CONTRACT_NORMAL_MONO_FULL_RANGE)
        and not contract.issues
    )


def active_topology_requires_roleful_graph(topology: OutputTopology) -> bool:
    return classify_output_contract(topology).requires_roleful_graph


def topology_sink_is_composite(topology: OutputTopology) -> bool:
    """True iff the saved topology's output sink spans MULTIPLE child DACs.

    Keyed on ``len(hardware.child_devices) >= 2`` — a PLURALITY of child DACs,
    each its own USB clock domain. A SINGLE child is the opposite: one coherent
    stereo sink on one clock, and the shipped-default dongle and hifiberry paths
    both populate ``child_devices=(card,)`` for stable serial identity, so that
    entry must NOT read as composite (a bare truthiness check here once
    misclassified every shipped-default box).
    """

    return len(topology.hardware.child_devices) >= 2


# The channel count Ring B carries for a ring-eligible topology. The rings move
# a full-range STEREO program: everything upstream of CamillaDSP is stereo
# (``mixer.rs``'s ``CHANNELS: u32 = 2``, "Not configurable"), and on a
# ring-eligible box CamillaDSP's output is the same stereo program. Named so the
# one place that decides ring width is greppable.
RING_STEREO_PROGRAM_CHANNELS = 2


def ring_channels_for_topology(topology: OutputTopology) -> int | None:
    """Channels Ring B would carry for this topology, or ``None`` if no ring can.

    The single ring-eligibility answer, phrased as a WIDTH rather than a
    boolean: the ring's four ends (fan-in, the two ioplug PCMs, outputd) must
    each declare the same geometry, and a yes/no predicate leaves every one of
    them to re-derive the number.

    Ring A/Ring B carry a full-range STEREO program on a single coherent ALSA
    sink, so :data:`RING_STEREO_PROGRAM_CHANNELS` is the answer for an explicit
    valid passive layout — stereo or MONO. **A mono BOX is not a mono SIGNAL
    PATH**: every ring end stays two channels wide on a mono cabinet (the fan-in
    mixer is 2-channel, CamillaDSP emits two with the complement hard muted, and
    outputd opens the DAC at stereo), because the fold lives in the GRAPH,
    downstream of them all.

    Everything else has no Ring B:

    - roleful / protected / subwoofer topologies. Their POST-crossover
      per-driver program rides the ACTIVE ring — its own PCM, file and width,
      answered by :func:`active_ring_channels_for_topology`. "No Ring B", not
      "a wider Ring B";
    - composite sinks, for WIDTH and program rather than transport: Ring B
      carries the full-range stereo program and a composite drives four outputs
      across two child DACs. A ROLEFUL composite's post-crossover program rides
      the ACTIVE ring; a PASSIVE stereo composite resolves neither ring and
      stays on loopback. Keyed on ``len(child_devices) >= 2`` — see
      :func:`topology_sink_is_composite` for why a single child must not
      disqualify the ring.
    """
    contract = classify_output_contract(topology)
    if contract.requires_roleful_graph:
        return None
    # Composite (dual-Apple, kind="composite") is excluded even when nominally
    # stereo: a MULTI-child sink spans >1 USB clock domain and is not the single
    # coherent L/R sink the ring drives.
    if topology_sink_is_composite(topology):
        return None
    # A declared passive full-range layout is the Ring-B shape, mono or stereo:
    # both drive one coherent sink with the same 2-channel program (see the
    # mono paragraph above). An empty topology is deliberately silent; treating
    # it as implicit stereo would give ``speaker_groups=[]`` two meanings.
    if contract.issues:
        return None
    if contract.classification in {
        CONTRACT_NORMAL_STEREO_FULL_RANGE,
        CONTRACT_NORMAL_MONO_FULL_RANGE,
    }:
        return RING_STEREO_PROGRAM_CHANNELS
    return None


def active_ring_channels_for_topology(topology: OutputTopology) -> int | None:
    """Channels the ACTIVE ring would carry for this topology, or ``None``.

    The width of the THIRD ring (``jts_ring_active_playback``), which carries a
    roleful box's POST-crossover per-driver program from CamillaDSP to outputd.
    A SEPARATE function from :func:`ring_channels_for_topology` rather than a
    widening of it: that one answers for Ring B and is stamped into the
    ``pcm.jts_ring_playback`` conf.d block, so returning the ACTIVE width there
    would land it in the STEREO ring's block — invisible on a 2-way box, where
    the active width is also 2. One field per ring end.

    Returns the COMMISSIONED active width — the roleful outputs the saved
    topology actually assigns — never the DAC profile's declared capability.

    A COMPOSITE roleful sink is ANSWERED here, not refused: the ring is the
    CamillaDSP → outputd hop and the composite split lives downstream of it,
    inside outputd. The duplicate / contiguity / accept-set guards below run
    unchanged, and on a saved dual-Apple ``active_2_way`` they see flat
    contiguous indices ``0..3`` and answer 4 — ``physical_output_index`` and
    ``child_devices[].physical_output_indexes`` are ONE flat index space, not
    child-relative.

    ``None`` — no active ring — for any topology that does not require a roleful
    graph, and for a roleful topology whose assignments do not resolve to a
    coherent contiguous width inside the ring layout's accept-set. Fail-CLOSED:
    an indeterminate width must never be stamped into a conf.d block the ioplug
    attach then compares field-by-field.
    """
    contract = classify_output_contract(topology)
    if not contract.requires_roleful_graph:
        return None
    indices = {
        int(item.physical_output_index)
        for item in contract.assignments
        if item.physical_output_index is not None
    }
    if len(indices) != len(contract.assignments) or not indices:
        # An unassigned output (or a duplicate index) leaves the driven width
        # indeterminate. Refuse rather than guess.
        return None
    width = max(indices) + 1
    if width != len(indices):
        # Non-contiguous assignment: outputs 0 and 2 with nothing at 1 is not a
        # width the emitted graph and the ring can both mean the same thing by.
        return None
    if not (MIN_RING_CHANNELS <= width <= MAX_RING_CHANNELS):
        return None
    return width


def subwoofer_output_indexes(contract: OutputContract) -> set[int]:
    """Physical output indices the saved topology assigns to a subwoofer role."""
    return {
        int(item.physical_output_index)
        for item in contract.assignments
        if item.role == "subwoofer" and item.physical_output_index is not None
    }


def mains_lowest_driver_indexes(contract: OutputContract) -> set[int]:
    """Physical output indices of each main side's LOWEST driver — the woofer for
    an active main, the single full-range driver for a passive main.

    These are the outputs that MUST carry the complementary bass-management
    high-pass when a local subwoofer is present. Derived from the saved topology's
    speaker mode + role, independently of the emitter's preset."""
    out: set[int] = set()
    for item in contract.assignments:
        if item.physical_output_index is None:
            continue
        if item.speaker_kind == "subwoofer" or item.speaker_mode == "subwoofer":
            continue
        if LOWEST_DRIVER_ROLE_BY_MAIN_MODE.get(item.speaker_mode) == item.role:
            out.add(int(item.physical_output_index))
    return out
