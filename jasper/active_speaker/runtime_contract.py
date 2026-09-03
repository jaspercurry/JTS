# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Runtime safety contract for roleful active-speaker CamillaDSP graphs.

One graph this module must reject (see `_flat_graph_allowed`): a flat
graph that maps full-range stereo directly to DAC outputs is illegal
when the saved output topology assigns any physical output to a
tweeter/protected role.

``jasper.output_topology`` owns the declarative physical-output contract.
This module owns the runtime question that follows from it: whether a
candidate or running CamillaDSP graph is legal for that exact saved topology,
and which graph install/reconcile paths may select when they need a safe
fallback. It is deliberately file-based and side-effect-free except for the
explicit statefile writer helper at the bottom.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import json
import hashlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import (
    TYPE_CHECKING, Any, Awaitable, Callable, Iterable, Literal, Mapping, Sequence,
)

import yaml

from jasper.atomic_io import atomic_write_text
from jasper.audio_measurement.evidence_identity import NormalizedActiveRawIdentity
from jasper.camilla_emit import mono_sum_sources
from jasper.log_event import log_event

if TYPE_CHECKING:
    from jasper.bass_extension.profile import BassExtensionProfile
from jasper.audio_measurement.null_walk import MAX_DSP_DELAY_US
from jasper.output_topology import (
    SUB_CROSSOVER_HZ_HI,
    SUB_CROSSOVER_HZ_LO,
    OutputTopology,
    OutputTopologyError,
    SpeakerChannel,
    SpeakerGroup,
    load_output_topology_strict,
)

from ._common import issue as _issue
from .startup_hold import staged_startup_hold_active
from .camilla_yaml import (
    BASELINE_HEADROOM_DB,
    BASELINE_LIMITER_CLIP_LIMIT_DB,
    MAX_LINEARIZATION_BOOST_DB,
    STARTUP_LIMITER_CLIP_LIMIT_DB,
    STARTUP_MUTE_GAIN_DB,
)
from .graph_evidence import (
    bass_management_hp_name as _bass_management_hp_name,
    channel_select_mixer_name as _channel_select_mixer_name,
    driver_baseline_gain_name as _baseline_gain_name,
    driver_baseline_limiter_name as _baseline_limiter_name,
    driver_delay_name as _driver_delay_name,
    driver_limiter_name,
    driver_linearization_peak_name as _linearization_peak_name,
    driver_linearization_shelf_name as _linearization_shelf_name,
    driver_linearization_taper_name as _linearization_taper_name,
    filter_params as _filter_params,
    filter_type as _filter_type,
    output_commission_mute_name as _commission_mute_name,
    protective_tweeter_hp_name,
    sub_baseline_gain_name as _sub_baseline_gain_name,
    sub_baseline_limiter_name as _sub_baseline_limiter_name,
    sub_lowpass_name as _sub_lowpass_name,
    sub_startup_limiter_name as _sub_startup_limiter_name,
)
from .graph_safety import (
    TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ,
    GraphView,
    bass_extension_block_valid,
    bass_management_corner_matched,
    filter_param_matches,
    float_matches as _float_matches,
    float_value as _float_value,
    mains_highpass_present,
    output_hard_muted_and_wired,
    output_terminally_muted,
    pipeline_contains_chain,
    sub_audible_guard_present,
    sub_guard_present,
    truthy_bool as _truthy_bool,
    tweeter_guard_present,
    view_from_yaml_dict,
)
from .environment import (
    CAMILLA_CLASS_ACTIVE_PARKED,
    CAMILLA_CLASS_PROGRAM_BAKE,
    DEFAULT_CAMILLA_STATEFILE,
    classify_camilla_config_text,
    parse_camilla_statefile_config_path,
    read_camilla_statefile_config_path,
)
from .path_safety import (
    software_guard_ready_for_startup,
    staged_target_signature,
    target_assignment_signature,
    topology_target_signature,
)
from .profile import (
    ADJACENT_PAIRS_BY_WAY,
    SUB_CROSSOVER_ORDER,
    SUPPORTED_LR_ORDERS,
)

logger = logging.getLogger(__name__)

# The ONE flat outputd startup graph. It is a RING graph: the ring is the only
# transport (ADR-0100), so there is no sibling to re-seed instead of.
DEFAULT_FLAT_OUTPUTD_CONFIG = Path("/etc/camilladsp/outputd-cutover.yml")
DEFAULT_CAMILLA2_STATEFILE = Path("/var/lib/camilladsp/crossover-statefile.yml")

GRAPH_FLAT_FULL_RANGE = "flat_full_range"
GRAPH_ALL_MUTED_ACTIVE_STARTUP = "all_muted_active_startup"
GRAPH_GUARDED_COMMISSIONING = "guarded_commissioning"
GRAPH_APPROVED_ACTIVE_RUNTIME = "approved_active_runtime"
_BASS_PROFILE_EVIDENCE_OMITTED = object()
GRAPH_DRIVER_DOMAIN_BASELINE = "driver_domain_baseline"
# The active-leader's camilla#1 program bake: a flat (no-Layer-A) program graph
# whose playback is a File/pipe sink, not a DAC. Allowed regardless of topology
# (safe by construction — no DAC, no driver to over-drive); see
# _flat_graph_allowed.
GRAPH_PROGRAM_BAKE_PIPE = "program_bake_pipe"
# The PARKED graph (issue #2135): a roleful/protected topology that has declared
# drivers but has not yet staged an all-muted active startup graph. Every
# physical output is hard-muted and no unmuted route exists from any capture
# channel to any playback channel, so it is legal for ANY topology — but it is a
# HOLDING state, never a tuning. It is deliberately NOT interchangeable with
# GRAPH_ALL_MUTED_ACTIVE_STARTUP: the staged graph carries real per-driver
# crossover/limiter/protective-HP wiring that survives an unmute, while a parked
# graph carries none of it and must therefore never be preserved in preference
# to a staged graph. See ``_parked_graph_allowed`` for the independent proof and
# ``safe_graph_for_current_topology`` for where it sits in the decision order
# (last, after every real graph has been considered).
GRAPH_PARKED_ALL_MUTED = "parked_all_muted"
GRAPH_UNKNOWN = "unknown"
GRAPH_UNSAFE = "unsafe"

# The third statefile-seeding outcome, alongside "select a flat graph" and
# "select the staged all-muted active startup graph". A parked deploy SUCCEEDS —
# `SafeGraphDecision.ok` is true — because holding a speaker silent until its
# saved intent permits a real output graph is a legal end state, not a failure.
PARKED_MUTED_STATUS = "parked_muted"
PARKED_MUTED_REASON = (
    "roleful/protected topology has no staged startup graph yet; "
    "parked with every output muted"
)
# The two exits out of parked, verbatim, so the CLI transcript, jasper-doctor,
# and /state all name the same two actions.
PARKED_MUTED_EXITS = (
    "finish crossover preview to stage a startup graph, "
    "or reset output setup and choose an explicit passive layout"
)
UNCONFIGURED_PARKED_EXIT = (
    "choose and save a mono or stereo speaker layout before turning audio on"
)
# ...except on a DAC that declares no active outputd lane, where the first exit
# is IMPOSSIBLE: commissioning can never produce a graph that reaches hardware
# there (jasper.active_speaker.playback_route.active_lane_capability_gap owns
# that predicate). Naming an impossible action first sends a household down a
# road with no end, so the capability-aware surfaces use this instead.
PARKED_MUTED_EXITS_NO_ACTIVE_LANE = (
    "reset output setup at /sound/setup/, then choose an explicit passive "
    "layout (passive sends full-range to every output and requires a built-in "
    "passive crossover), "
    "or attach an active-capable DAC"
)


def parked_muted_exits(topology: OutputTopology | None = None) -> str:
    """The exits out of parked that are actually reachable on this hardware.

    Fail-soft: any unreadable topology falls back to the general pair rather
    than raising inside a reporting surface.
    """

    from jasper.active_speaker.playback_route import (
        ActiveLaneCapabilityGap,
        active_lane_capability_gap,
    )

    try:
        resolved = topology or load_output_topology_strict()
        if classify_output_contract(resolved).classification == CONTRACT_UNCONFIGURED:
            return UNCONFIGURED_PARKED_EXIT
        gap = active_lane_capability_gap(resolved)
    except (OutputTopologyError, OSError, ValueError, TypeError, KeyError):
        return PARKED_MUTED_EXITS
    # An unrecognized DAC profile is not proof the active lane is impossible —
    # see active_lane_capability_gap's docstring — so it takes the same exit
    # as a genuinely capable DAC, not the no-active-lane one.
    if not isinstance(gap, ActiveLaneCapabilityGap):
        return PARKED_MUTED_EXITS
    return f"{gap.device_label} cannot drive an active speaker layout — {PARKED_MUTED_EXITS_NO_ACTIVE_LANE}"

# Explicit evidence for frozen in-memory tests/composition inputs that prove an
# ordinary no-profile baseline. Production persisted hosts obtain the same shape
# only through :func:`classify_bass_extension_graph`.
NO_BASS_EXTENSION_PROFILE_SUMMARY: Mapping[str, Any] = MappingProxyType({
    "authority_valid": True,
    "runtime_block_required": False,
})

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
_DRIVER_DOMAIN_PAIR_TRIM = "pair_balance_trim"
# Both emitted baseline-shaped sources run every output live through a
# protective per-driver chain; they differ only in the pre-split prefix
# (program-domain headroom + preference EQ vs inter-speaker channel-select).
# Summed commissioning may derive a narrowly verified final mute tail from the
# primary baseline source; the driver-domain source never may.
_BASELINE_LIKE_SOURCES = (ACTIVE_BASELINE_SOURCE, ACTIVE_DRIVER_DOMAIN_SOURCE)

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

# Stable refusal codes for a flat full-range program graph.  The runtime
# contract owns these machine-readable decisions; callers may add their own
# household-facing prose, but must never infer policy from that prose.
FlatProgramGraphBlockCode = Literal[
    "flat_graph_unconfigured",
    "flat_graph_not_authorized",
    "flat_graph_protected_tweeter",
]
FlatProgramGraphBlock = tuple[FlatProgramGraphBlockCode, str]
FLAT_PROGRAM_GRAPH_UNCONFIGURED: FlatProgramGraphBlockCode = "flat_graph_unconfigured"
FLAT_PROGRAM_GRAPH_NOT_AUTHORIZED: FlatProgramGraphBlockCode = (
    "flat_graph_not_authorized"
)
FLAT_PROGRAM_GRAPH_PROTECTED_TWEETER: FlatProgramGraphBlockCode = (
    "flat_graph_protected_tweeter"
)

# The snd-aloop ACTIVE lane's playback PCM — RETIRED as an endpoint. #2534
# deleted its PCM definitions; this change deletes its MEMBERSHIP below, so no
# graph naming it can be a legal outputd endpoint any more.
#
# The name survives for the TESTS, and that is the whole reason — measured, not
# assumed. Production readers of this constant: ZERO (the only other mention in
# `jasper/` is the comment below). The refusal is a SET-COMPLEMENT —
# `resolve_live_active_endpoint` asks `named in OUTPUTD_LEGAL_ENDPOINT_DEVICES`
# and declines everything else — so no guard names this device at all; an
# earlier version of this comment claimed one did, which was the opposite of
# what the code does.
#
# What still reads it is the suite: 39 references across five modules pin that a
# graph persisted before the retirement, or a stale hand-rolled asoundrc, is
# REFUSED by name. Deleting the constant would make those tests spell the retired
# device as a bare literal, which is strictly worse than one named constant.
# Deleting it would also not remove the string from the tree: the sibling
# `jasper.camilla_config_contract.ACTIVE_OUTPUTD_PLAYBACK_DEVICE` holds the same
# literal and does have live production readers.
OUTPUTD_ACTIVE_PLAYBACK_DEVICE = "outputd_active_content_playback"
# Every playback device a legal outputd ENDPOINT graph may name. ONE member:
# the ACTIVE RING is now the only transport carrying POST-crossover per-driver
# channels to outputd. It stays a frozenset rather than collapsing to a single
# `==` because membership is the seam the endpoint width probe reads — it must
# reject everything outside this set, notably the STEREO ring (which carries a
# full-range program no active graph may target) and the retired snd-aloop lane
# above.
#
# Redeclared (deliberately, like OUTPUTD_ACTIVE_PLAYBACK_DEVICE itself) rather
# than imported from jasper.fanin_coupling: this module is the runtime
# VERIFIER's independent copy of the endpoint vocabulary, and the contract test
# pins the copies equal.
OUTPUTD_ACTIVE_RING_PLAYBACK_DEVICE = "jts_ring_active_playback"
OUTPUTD_LEGAL_ENDPOINT_DEVICES = frozenset((
    OUTPUTD_ACTIVE_RING_PLAYBACK_DEVICE,
))
OUTPUTD_ENDPOINT_GRAPH_CLASSIFICATIONS = frozenset((
    GRAPH_ALL_MUTED_ACTIVE_STARTUP,
    GRAPH_GUARDED_COMMISSIONING,
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    GRAPH_DRIVER_DOMAIN_BASELINE,
    # GRAPH_PARKED_ALL_MUTED is deliberately ABSENT: a parked graph's sink is a
    # File, not the active outputd lane, so it is not an outputd endpoint and
    # outputd must not open the DAC's active lane for it.
))


@dataclass(frozen=True)
class OutputAssignment:
    speaker_group_id: str
    speaker_label: str
    speaker_kind: str
    speaker_mode: str
    role: str
    physical_output_index: int | None
    identity_verified: bool
    startup_muted: bool
    protection_required: bool
    protection_status: str

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
            "physical_output_index": self.physical_output_index,
            "identity_verified": self.identity_verified,
            "startup_muted": self.startup_muted,
            "protection_required": self.protection_required,
            "protection_status": self.protection_status,
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


@dataclass(frozen=True)
class GraphSafety:
    classification: str
    allowed: bool
    config_path: str | None = None
    camilla_classification: str = "missing"
    playback_device: str | None = None
    playback_channels: int | None = None
    issues: tuple[dict[str, str], ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "allowed": self.allowed,
            "config_path": self.config_path,
            "camilla_classification": self.camilla_classification,
            "playback_device": self.playback_device,
            "playback_channels": self.playback_channels,
            "issues": list(self.issues),
            "details": self.details,
        }


@dataclass(frozen=True)
class SafeGraphDecision:
    """The graph the runtime contract selects for the saved topology.

    ``selected_config_path`` normally names a config that ALREADY EXISTS on
    disk. The one exception is ``status == PARKED_MUTED_STATUS`` (#2135): the
    parked graph is *generated*, so the path names where
    ``apply_safe_graph_decision_to_statefile`` will materialise it. A read-only
    caller (one that does not write the statefile) must not assume the file is
    there yet.
    """

    status: str
    selected_config_path: str | None
    reason: str
    topology_contract: OutputContract
    current_graph: GraphSafety | None = None
    preferred_graph: GraphSafety | None = None
    fallback_graph: GraphSafety | None = None
    issues: tuple[dict[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return self.status != "blocked" and self.selected_config_path is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "selected_config_path": self.selected_config_path,
            "reason": self.reason,
            "ok": self.ok,
            "topology_contract": self.topology_contract.to_dict(),
            "current_graph": (
                self.current_graph.to_dict() if self.current_graph else None
            ),
            "preferred_graph": (
                self.preferred_graph.to_dict() if self.preferred_graph else None
            ),
            "fallback_graph": (
                self.fallback_graph.to_dict() if self.fallback_graph else None
            ),
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class OutputdActiveLaneDecision:
    """Whether outputd may open its active content lane, and at what width.

    ``endpoint_device`` names WHICH legal endpoint the accepted graph targets —
    the ALSA active lane or the active ring. It exists so the reconciler's
    ring-endpoint marker derives from THIS decision rather than from a second
    read of the same graph: two independent classifications of one graph is how
    the marker and the width would come to describe different things. ``None``
    whenever the decision is not ``ok`` (there is no accepted endpoint to name).
    """

    ok: bool
    width: int | None
    reason: str
    source: str | None = None
    primary_graph: GraphSafety | None = None
    endpoint_graph: GraphSafety | None = None
    endpoint_device: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "width": self.width,
            "reason": self.reason,
            "source": self.source,
            "endpoint_device": self.endpoint_device,
            "primary_graph": (
                self.primary_graph.to_dict() if self.primary_graph else None
            ),
            "endpoint_graph": (
                self.endpoint_graph.to_dict() if self.endpoint_graph else None
            ),
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
        physical_output_index=channel.physical_output_index,
        identity_verified=bool(channel.identity_verified),
        startup_muted=bool(channel.startup_muted),
        protection_required=bool(channel.protection_required),
        protection_status=channel.protection_status,
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


def roleful_identity_confirmed(
    topology: OutputTopology,
    contract: OutputContract | None = None,
) -> bool:
    """Whether every ASSIGNED lane of a ROLEFUL topology is confirmed by ear.

    The one place this fact is stated, because two owners need the same answer:
    :func:`safe_graph_for_current_topology` (which graph may be selected) and
    the ``/sound/setup/`` identity endpoint (whether a write must park the
    speaker). It reads the topology directly — the household's confirmation
    lives there and nowhere else, so there is no marker file to drift.

    Scope is deliberate and narrow:

    * **Roleful only.** A passive full-range topology carries no crossover, so
      an unconfirmed lane is a channel-swap annoyance, not a driver hazard.
      ``requires_roleful_graph`` False answers True here and the flat rungs are
      untouched.
    * **Assigned lanes only.** An unassigned channel has no physical output to
      confirm; it is already a topology blocker in its own right.

    This is the UNVERIFIED direction. Its mirror —
    ``test_safe_graph_preserves_staged_startup_after_identity_confirmation`` —
    guards the CONFIRM direction, that flipping a lane back to verified must
    not bounce a staged all-muted graph. The two do not meet: that test lands on
    the ``GRAPH_ALL_MUTED_ACTIVE_STARTUP`` rung, which this predicate never
    gates, and it reads a stale flag on the staged METADATA while this reads the
    topology. Only the two approved-active-runtime rungs consult this.
    """

    contract = contract or classify_output_contract(topology)
    if not contract.requires_roleful_graph:
        return True
    return all(
        channel.identity_verified
        for group in topology.speaker_groups
        for channel in group.channels
        if channel.physical_output_index is not None
    )


def topology_sink_is_composite(topology: OutputTopology) -> bool:
    """True iff the saved topology's output sink spans MULTIPLE child DACs.

    Keyed on ``len(hardware.child_devices) >= 2`` — a *plurality* of child DACs,
    each its own USB clock domain (``dac.py``'s only ``kind="composite"``
    profile is the dual-Apple 4-ch, ``child_profile_ids=(apple, apple)``). A
    *single* child (``len == 1``) is the opposite: one coherent stereo sink on
    one clock. The shipped-default dongle and hifiberry paths both populate
    ``child_devices=(card,)`` for stable serial identity, so that single entry
    must NOT read as composite — pre-2026-07 this was written as a bare
    ``if child_devices:`` truthiness check, which wrongly classified every
    shipped-default box (DEFECT 2 in ``topology_supports_shm_ring``).

    Two callers need this same distinction for different reasons, so it is named
    once here rather than spelled twice: ``topology_supports_shm_ring`` (the
    STEREO ring carries a full-range stereo program, which a 4-ch composite is
    not — a ROLEFUL composite's post-crossover program rides the ACTIVE ring
    instead, see :func:`active_ring_channels_for_topology`) and
    ``flat_graph_program_dest_map`` (outputd fans the stereo program across
    child DACs, so program channel *i* is not physical output *i* — it is the
    single output that child *i* declares).
    """

    return len(topology.hardware.child_devices) >= 2


# The channel count Ring B carries for a ring-eligible topology. The rings move
# a full-range STEREO program: everything upstream of CamillaDSP is stereo (the
# fan-in mixer is 2-channel and says so — ``mixer.rs``'s ``CHANNELS: u32 = 2``,
# "Not configurable"), and on a ring-eligible box CamillaDSP's output is the
# same stereo program. Named rather than spelled ``2`` at each site so the one
# place that decides ring width is greppable.
RING_STEREO_PROGRAM_CHANNELS = 2


def ring_channels_for_topology(topology: OutputTopology) -> int | None:
    """Channels Ring B would carry for this topology, or ``None`` if no ring can.

    The single ring-eligibility answer, phrased as a WIDTH rather than a
    boolean: the ring's four ends (fan-in, the two ioplug PCMs, outputd) must
    each declare the same geometry, and a predicate that only says yes/no leaves
    every one of them to re-derive the number. ``None`` means no ring geometry
    exists for this topology at all — :func:`topology_supports_shm_ring` is
    derived from exactly that.

    The topology-contract citizenship for rings (audio-graph consolidation P2):
    Ring A/Ring B carry a full-range **stereo** program on a single coherent
    ALSA sink, so :data:`RING_STEREO_PROGRAM_CHANNELS` is the answer for an
    explicit, valid passive layout — stereo or MONO. An UNCONFIGURED topology
    has no declared speaker layout and remains parked, so it has no Ring B.

    **Mono rides the stereo ring: a mono BOX is not a mono SIGNAL PATH.** Every
    ring end stays two channels wide on a mono cabinet — the fan-in mixer is
    2-channel, CamillaDSP emits two (both program channels folded onto the one
    declared output, complement hard muted), and the reconciler clears
    ``JASPER_OUTPUTD_ACTIVE_CHANNELS`` for a non-active box so outputd opens the
    DAC at stereo. The fold lives in the GRAPH, downstream of them all. The
    exclusion this replaces was right that a 1-channel ring is not
    representable, and wrong that a mono box wants one.

    Everything else has no Ring B:

    - roleful / protected / subwoofer topologies (``requires_roleful_graph``).
      Their POST-crossover per-driver program rides the ACTIVE ring — its own
      PCM, its own file and its own width, answered by
      :func:`active_ring_channels_for_topology` and gated by outputd's endpoint
      marker. So the answer here is "no Ring B", not "a wider Ring B";
    - composite sinks (dual-Apple — TWO+ ``hardware.child_devices``). **The
      reason is WIDTH and program, not the transport.** Ring B carries the
      full-range STEREO program; a composite drives four physical outputs across
      two child DACs, which is not a stereo program, so there is no Ring B for
      it — the same "no Ring B, not a wider Ring B" answer the roleful bullet
      above gives. What is NOT the reason, since P8b item 1b: that the ring
      ioplug cannot serve a composite. It can — the ring is the CamillaDSP →
      outputd hop and the composite split lives downstream of it, so a ROLEFUL
      composite's post-crossover program rides the ACTIVE ring, answered by
      :func:`active_ring_channels_for_topology`. A PASSIVE stereo composite
      resolves neither ring and stays on loopback. The exclusion is
      keyed on ``len(child_devices) >= 2`` — a *plurality* of child DACs, each its
      own USB clock domain (``dac.py``'s only ``kind="composite"`` profile is the
      dual-Apple 4-ch, ``child_profile_ids=(apple, apple)``). A *single* child
      (``len == 1``) is the opposite: one coherent stereo sink on one clock, which
      IS exactly what the ring drives — the single-Apple-dongle and single
      registered DAC (hifiberry) paths both populate ``child_devices=(card,)`` for
      stable serial identity, and that single entry must NOT disqualify the ring.
      (Pre-2026-07 this read ``if child_devices:`` — a bare truthiness check that
      wrongly refused every shipped-default box, since observed hardware always
      records its one child. See DEFECT 2.).
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

    The width of the THIRD ring (``jts_ring_active_playback`` /
    ``active-content.ring``), which carries a roleful box's POST-crossover
    per-driver program from CamillaDSP to outputd. It is deliberately a SEPARATE
    function from :func:`ring_channels_for_topology` rather than a widening of
    it, and the separation is the whole point:

    - ``ring_channels_for_topology`` answers for **Ring B**, the full-range
      stereo program, and must keep returning ``None`` for a roleful topology.
      Its answer is stamped into the ``pcm.jts_ring_playback`` conf.d block. If
      it returned the ACTIVE width for a roleful box, that width would land in
      the STEREO ring's block — and on a 2-way box, where the active width is
      also 2, the corruption is numerically invisible.
    - This function answers for the **active ring** only. One field per ring
      end; never one field for two ends.

    Returns the topology's COMMISSIONED active width — the number of roleful
    outputs the saved topology actually assigns — not the DAC profile's declared
    capability. jts3's DAC8x declares an 8-channel active-lane capability while
    its commissioned graph drives 2; the ring is built to what is driven.

    **A COMPOSITE (multi-child) ROLEFUL SINK IS ANSWERED HERE, NOT REFUSED**
    (P8b item 1b). It is the one shape this function ever excluded for a reason
    that did not survive re-derivation. The old exclusion said "the ring ioplug
    is a single coherent device spanning one clock domain, which a multi-child
    composite is not" — but the ring is the **CamillaDSP → outputd** hop, and the
    composite split lives entirely DOWNSTREAM of it, inside outputd, which reads
    one interleaved period and calls ``deinterleave_4ch_to_dual_stereo``. The
    ring never sees a child. The transport it replaces — the raw snd-aloop
    lane — is equally one device on one clock domain and carries this exact
    composite in production today. The exclusion was right as a ring-v2 SCOPE
    call and wrong as a physics claim.

    What the composite arm does NOT do is relax anything: it deletes the early
    refusal and lets the duplicate / contiguity / accept-set guards below run
    unchanged. On a real saved dual-Apple ``active_2_way`` those guards see flat
    contiguous indices ``0..3`` and answer **4** — because
    ``speaker_groups[].channels[].physical_output_index`` and
    ``hardware.child_devices[].physical_output_indexes`` are ONE flat index
    space, not child-relative: ``OutputTopology._validate_references`` bounds the
    channel index by ``hardware.physical_output_count`` (4 for this profile),
    ``topology_hardware_from_state`` assigns child ordinal *i* the pair
    ``[2i, 2i+1]``, ``_dual_apple_clock_issues`` BLOCKS any composite whose child
    indexes are not ``range(4)`` exactly once, and
    :func:`jasper.output_topology.cross_child_group_verdicts` looks a channel's
    index up directly in the child-owned map. So no child-ordinal composition is
    needed here; had the indices been child-relative the duplicate guard would
    have fired (two outputs both claiming 0) and this would return ``None`` with
    a named refusal rather than stamp a wrong width.

    Ring B stays excluded for a composite — see
    :func:`ring_channels_for_topology`. One field per ring end.

    ``None`` — no active ring — for:

    - any topology that does not require a roleful graph. Those boxes have no
      active ring. An explicit passive stereo single sink may use Ring B;
      explicit mono, unconfigured, invalid, and passive composite layouts use
      neither ring. A PASSIVE stereo composite requires no roleful graph, so it
      gets no active ring here and no Ring B there — it stays on loopback;
    - a roleful topology whose assignments do not resolve to a coherent
      contiguous output width (an output with no assigned physical index, or a
      declared roleful set the ring layout's ``2..=8`` accept-set cannot carry).
      Fail-CLOSED: an indeterminate width must never be stamped into a conf.d
      block that the ioplug attach then compares field-by-field.
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


# The ring layout's channel accept-set (``jasper_ring::Geometry::validate_self``
# and the C ioplug's ``JTS_RING_MIN_CHANNELS`` / ``MAX_RING_CHANNELS``). Spelled
# here so the topology side refuses a width the transport could not carry
# instead of deferring it to an attach failure.
MIN_RING_CHANNELS = 2
MAX_RING_CHANNELS = 8


def topology_supports_shm_ring(topology: OutputTopology) -> bool:
    """True iff the saved topology can be driven by the STEREO ``shm_ring`` coupling.

    DERIVED from :func:`ring_channels_for_topology` — a topology is
    stereo-ring-eligible exactly when a Ring B width exists for it. Two functions
    answering the same question independently is how the boolean and the width
    would drift; the reasons live in that function's docstring.

    **This stays False for a roleful topology, and widening it is forbidden.**
    A roleful box's ring is the ACTIVE ring, which is a different transport with
    a different width, a different device name and a different file — reached
    through :func:`active_ring_channels_for_topology` and the endpoint marker,
    never by making this predicate say yes. Two consumers make the one-liner
    dangerous: the unattended ``--auto`` default pass would AUTO-ARM every
    roleful box in the fleet through gates that would then pass, and
    ``jasper.sound.camilla_yaml``'s flat-cutover defusal gate protects exactly
    the boxes a widened predicate would re-expose. Pinned by
    ``tests/test_ring_active_endpoint.py``.

    The real consumers are TWO: ``jasper.fanin.coupling_reconcile``'s
    ``ring_topology_ready`` (the arm preflight) and
    :func:`safe_graph_for_current_topology` (the graph seeder's flat branch).
    ``resolve_auto_decision`` reaches it only transitively through the injected
    ``ring_topology`` gate, and multiroom bond-formation checks the PERSISTED
    coupling value rather than this predicate."""
    return ring_channels_for_topology(topology) is not None


def flat_program_graph_block(
    topology: OutputTopology | None = None,
) -> FlatProgramGraphBlock | None:
    """Typed refusal for a flat full-range *program* graph, or ``None``.

    The program lane (``jasper.sound.camilla_yaml.emit_sound_config`` and the
    ``/sound`` / correction callers it backs) emits a 2-channel passthrough with
    no per-driver crossover or protection. It may reach the DAC only for one
    complete explicit passive mono/stereo layout. Unconfigured, invalid,
    subwoofer, and roleful/protected layouts are all blocked; the latter would
    otherwise send full range to a compression-driver tweeter.

    Returns one stable refusal code plus household-readable detail when the
    graph is blocked, or ``None`` only when
    :func:`topology_allows_flat_dac_graph` permits it. Policy callers branch on
    the code; prose is presentation only. This is a *topology* predicate, not a
    graph check. Verifying a graph that should be protective — an active
    baseline — is :func:`classify_camilla_graph`'s job.

    Fail-closed: a corrupt/unreadable saved topology returns a reason (block)
    rather than raising, so a caller can never read "safe" out of a topology it
    could not load. Callers own the policy and the structured logging:
    :class:`jasper.sound.graph_carrier.CarrierCannotHostEq` for ``/sound``,
    ``CorrectionRuntimeSafetyError`` for room correction.
    """
    try:
        contract = classify_output_contract(topology or load_output_topology_strict())
    except OutputTopologyError as exc:
        return (
            FLAT_PROGRAM_GRAPH_NOT_AUTHORIZED,
            f"the saved output topology is unavailable or invalid ({exc})",
        )
    if topology_allows_flat_dac_graph(contract):
        return None
    if contract.classification == CONTRACT_UNCONFIGURED:
        return FLAT_PROGRAM_GRAPH_UNCONFIGURED, "no speaker layout is configured"
    if any(item.role == "tweeter" for item in contract.protected_assignments):
        return FLAT_PROGRAM_GRAPH_PROTECTED_TWEETER, _protected_output_detail(contract)
    if not contract.requires_roleful_graph:
        detail = "saved topology is not a complete passive mono or stereo layout"
    else:
        detail = _protected_output_detail(contract)
    return FLAT_PROGRAM_GRAPH_NOT_AUTHORIZED, detail


def flat_program_graph_blocked_reason(
    topology: OutputTopology | None = None,
) -> str | None:
    """Household-readable flat-program refusal detail, or ``None``."""

    block = flat_program_graph_block(topology)
    return block[1] if block is not None else None


def _statefile_config_path(statefile_path: str | Path | None) -> str | None:
    return read_camilla_statefile_config_path(statefile_path)


def _read_text(path: str | Path) -> tuple[str | None, dict[str, str] | None]:
    try:
        return Path(path).read_text(encoding="utf-8"), None
    except OSError as exc:
        return None, _issue(
            "blocker",
            "camilla_config_unreadable",
            f"could not read CamillaDSP config {path}: {type(exc).__name__}",
        )


def _path_matches(left: str | Path | None, right: str | Path | None) -> bool:
    if not left or not right:
        return False
    try:
        return Path(left).expanduser().resolve(strict=False) == Path(right).expanduser().resolve(strict=False)
    except OSError:
        return str(left) == str(right)


def _protected_output_detail(contract: OutputContract) -> str:
    targets = contract.protected_assignments or contract.roleful_assignments
    labels = [
        f"{item.output_label} ({item.role}{'/protected' if item.protected else ''})"
        for item in targets
    ]
    return ", ".join(labels) or "a roleful/protected output"


def _playback_is_program_bake_pipe(text: str) -> bool:
    """True iff a flat graph's ``devices.playback`` is the snapserver File pipe
    the active-leader's camilla#1 program bake writes.

    This is the load-bearing key for the program-bake exemption: a ``File`` sink
    has no DAC, so no driver can be over-driven — safe regardless of topology.
    It reuses :func:`jasper.multiroom.leader_config.playback_is_pipe` (and the
    same ``SNAPFIFO`` target) verbatim so this exemption and the leader-pipe
    liveness check cannot disagree about what "pipe-shaped" means. Both symbols
    are imported lazily — they live in the grouping reconciler chain, which this
    read-heavy module must not pull eagerly (the leader_config sibling uses the
    same lazy-import idiom)."""
    from jasper.multiroom.leader_config import playback_is_pipe
    from jasper.multiroom.reconcile import SNAPFIFO

    return playback_is_pipe(text, SNAPFIFO)


def flat_full_range_outputs(contract: OutputContract) -> frozenset[int]:
    """The physical outputs a flat full-range graph is allowed to emit on.

    The saved topology's ``full_range`` assignments and nothing else. This is
    the ONE definition of "which outputs has the household declared for the
    flat lane": :func:`_flat_graph_allowed` refuses a graph that emits outside
    it, and :func:`flat_graph_muted_outputs` — which the flat renderer calls —
    derives the complement it must hard-mute from the same set. Keeping both
    sides on one function is what makes the renderer's mute and the checker's
    demand incapable of disagreeing.
    """

    return frozenset(
        item.physical_output_index
        for item in contract.assignments
        if item.role == "full_range" and item.physical_output_index is not None
    )


def flat_graph_program_dest_map(
    topology: OutputTopology,
    contract: OutputContract,
    *,
    width: int,
) -> tuple[int, ...] | None:
    """Which playback channel each program channel drives, or ``None``.

    The ONE answer to "where does the flat graph put the program". The renderer
    builds its mixer, its per-dest chains and its mutes from this; the checker
    asks it whether a live channel reached an undeclared output. One derivation
    is what keeps those two incapable of disagreeing.

    Playback-channel index and physical-output index are ONE space: a
    composite's children own a contiguous pair each
    (``hardware.child_devices[].physical_output_indexes``) and outputd
    deinterleaves in that order, so an entry is both a dest and an output.

    Two shapes resolve:

    * **indexed** — not a composite, every claimed output inside ``width``:
      program channel *i* drives output *i*.
    * **composite-paired** — a multi-child sink whose children declare exactly
      ONE ``full_range`` output each, all inside ``width``: program channel *i*
      drives child *i*'s output. A dual-Apple stereo box sits on outputs 0 and 2
      this way, which is exactly why the identity answer is wrong for it.

    ``None`` is UNDECIDED and both callers fail closed on it: the renderer mutes
    and folds nothing, and :func:`_flat_graph_allowed` refuses a graph wider than
    the program rather than guess where its live channels landed.
    """

    from jasper.sound.camilla_yaml import FLAT_PROGRAM_WIDTH

    if width < FLAT_PROGRAM_WIDTH:
        return None
    claimed = flat_full_range_outputs(contract)
    if not claimed or not claimed <= frozenset(range(width)):
        return None
    if not topology_sink_is_composite(topology):
        return tuple(range(FLAT_PROGRAM_WIDTH))
    children = topology.hardware.child_devices
    if len(children) != FLAT_PROGRAM_WIDTH:
        return None
    dests: list[int] = []
    for child in children:
        owned = sorted(claimed.intersection(child.physical_output_indexes))
        if len(owned) != 1:
            return None
        dests.append(owned[0])
    return tuple(dests)


def flat_graph_muted_outputs(
    topology: OutputTopology | None = None,
    *,
    width: int,
) -> frozenset[int]:
    """Playback channels a ``width``-wide flat graph must hard-mute.

    The flat emitter routes each program channel to the physical output
    :func:`flat_graph_program_dest_map` names for it, so every channel the saved
    topology does not claim as ``full_range`` would otherwise send full-range
    program to an output the household never declared — a mis-wired or
    undeclared driver receiving full range. Muting them is how the
    flat lane satisfies "no emission on undeclared outputs" BY CONSTRUCTION;
    :func:`_flat_graph_allowed` then re-proves it structurally off the emitted
    YAML rather than trusting the emitter.

    Muting is index-wise, so it is withheld unless
    :func:`flat_graph_program_dest_map` resolves where the program actually
    lands — muting by a mapping nobody has established would silence a working
    speaker.

    Returns EMPTY — mute nothing — when that fails, and for three more cases
    where the flat lane has no business silencing anything:

    * **unconfigured** topology (no speaker groups): the renderer may still
      produce the byte-identical stereo artifact, but runtime selection refuses
      it and parks the speaker because no output is declared.
    * **roleful/protected** topology: the flat graph is illegal there whatever
      is muted, and refusing it is :func:`_flat_graph_allowed`'s job (issue
      #2145 owns making that refusal park instead of abort). Silencing channels
      here would only disguise it.
    * **every channel unclaimed**: muting all of them would ship a silently
      silent speaker. Emitting the unmuted graph instead leaves the checker to
      refuse it with an operator-readable reason — the fail-loud direction.

    A corrupt/unreadable topology also returns empty: the renderer must not
    guess, and the graph it emits is still checked — ``classify_camilla_graph``
    and ``safe_graph_for_current_topology`` both fail closed on that topology,
    so the deploy still stops, at the layer that can say why.
    """

    if width <= 0:
        return frozenset()
    try:
        if topology is None:
            topology = load_output_topology_strict()
        contract = classify_output_contract(topology)
    except OutputTopologyError:
        return frozenset()
    if not contract.topology_configured or contract.requires_roleful_graph:
        return frozenset()
    if flat_graph_program_dest_map(topology, contract, width=width) is None:
        return frozenset()
    return frozenset(range(width)) - flat_full_range_outputs(contract)


def _flat_output_terminally_muted(
    payload: Mapping[str, Any],
    view: GraphView,
    index: int,
) -> bool:
    """This module's binding of :func:`output_terminally_muted` for a flat graph.

    The three-fact proof itself was PROMOTED to ``graph_safety`` when a second
    caller appeared — the ring arm's anchor acceptance
    (``jasper.fanin.coupling_reconcile._anchor_is_all_muted``) needs the same
    three facts about the same shape of graph, and a mirrored copy would be a
    drift site on a hearing-safety path. What stays here is the binding this
    module owns: the flat graph's commission-mute NAME for ``index`` and the
    startup mute floor. Behaviour is unchanged.
    """

    return output_terminally_muted(
        payload,
        view,
        index,
        mute_name=_commission_mute_name(index),
        mute_gain_db=STARTUP_MUTE_GAIN_DB,
    )


def _flat_hard_muted_outputs(text: str, playback_channels: Any) -> frozenset[int]:
    """The flat graph's terminally-muted playback channels, parsed from ``text``.

    Derived at ``classify_camilla_graph``'s scope, where the config text lives,
    so :func:`_flat_graph_allowed` stays text-free — the same split the
    ``program_bake_pipe`` fact already uses. Fails closed (empty set) on an
    unparseable graph, which leaves every channel counted as emitting.
    """

    if not isinstance(playback_channels, int) or isinstance(playback_channels, bool):
        return frozenset()
    if playback_channels <= 0:
        return frozenset()
    try:
        payload = yaml.safe_load(text)
    except (RecursionError, UnicodeError, ValueError, yaml.YAMLError):
        return frozenset()
    if not isinstance(payload, dict):
        return frozenset()
    view = view_from_yaml_dict(payload)
    return frozenset(
        index
        for index in range(playback_channels)
        if _flat_output_terminally_muted(payload, view, index)
    )


# The flat family's one Mixer step. Its emitter of record,
# ``jasper.camilla_emit.emit_master_gain_pipeline``, hard-codes the same literal
# for the same reason: the name IS the byte contract of every flat config in the
# field, so there is nothing to parameterise.
_MASTER_GAIN_MIXER = "master_gain"


def _required_mono_fold_output(
    topology: OutputTopology, *, playback_channels: Any
) -> int | None:
    """The playback channel a flat graph on ``topology`` MUST fold onto, if any.

    Delegated WHOLE to ``jasper.sound.camilla_yaml.flat_graph_channel_plan`` —
    the renderer's own answer to "which channel folds where" — so the checker
    cannot demand a fold the renderer would not emit, nor accept a box the
    renderer would have folded. Every case the plan withholds the fold for
    (unconfigured, roleful/protected, corrupt, composite sink) is withheld here
    too, without this side re-deriving, or drifting from, any of those rules.

    Imported lazily, mirroring :func:`_playback_is_program_bake_pipe`: the
    active-speaker package pulls ``jasper.sound.camilla_yaml`` at module scope,
    so a top-level edge back would be circular.

    ``None`` when the graph's own width is unreadable or non-positive. A plan
    derived at width 0 is degenerate — its mute set and the complement of the
    assigned output are both empty, which reads as "fold" — and a graph whose
    width cannot be read is already refused on its own summary issues.
    """

    if not isinstance(playback_channels, int) or isinstance(playback_channels, bool):
        return None
    if playback_channels <= 0:
        return None
    from jasper.sound.camilla_yaml import flat_graph_channel_plan

    return flat_graph_channel_plan(topology, width=playback_channels).mono_fold_output


def _flat_mono_fold_proved(text: str, fold_output: int) -> bool:
    """True iff ``text``'s ``master_gain`` mixer really folds L+R onto
    ``fold_output``, at the clip-safe gain, and really runs.

    Derived at ``classify_camilla_graph``'s scope like the mute set, so
    :func:`_flat_graph_allowed` stays text-free. Three facts, all required:

    * the pipeline's Mixer steps are exactly one un-bypassed ``master_gain``. A
      mixer the pipeline never runs folds nothing, and a second Mixer could
      re-route what this one summed. ``bypassed:`` is read for the same reason
      the mute proof reads it — CamillaDSP skips the step entirely, so the
      mapping below would otherwise attest a fold that never happens;
    * that mixer feeds ``fold_output`` from BOTH program channels, neither
      source muted, order-free (a mixer is a sum);
    * each feed carries :data:`~jasper.camilla_emit.MONO_SUM_GAIN_DB` and its
      polarity. The gain is contract, not decoration: two unity feeds sum 6 dB
      hotter, and a mono track then clips against ``volume_limit: 0.0``.

    Fails closed on anything unparseable or unexpected.
    """

    try:
        payload = yaml.safe_load(text)
    except (RecursionError, UnicodeError, ValueError, yaml.YAMLError):
        return False
    if not isinstance(payload, dict):
        return False
    pipeline = payload.get("pipeline")
    steps = [
        step
        for step in (pipeline if isinstance(pipeline, list) else [])
        if isinstance(step, dict) and step.get("type") == "Mixer"
    ]
    if len(steps) != 1 or steps[0].get("name") != _MASTER_GAIN_MIXER:
        return False
    if _truthy_bool(steps[0].get("bypassed")):
        return False
    mixers = payload.get("mixers")
    mixer = mixers.get(_MASTER_GAIN_MIXER) if isinstance(mixers, dict) else None
    if not isinstance(mixer, dict):
        return False
    mapping = mixer.get("mapping")
    if not isinstance(mapping, list):
        return False
    entries = [
        entry
        for entry in mapping
        if isinstance(entry, dict)
        and type(entry.get("dest")) is int
        and entry["dest"] == fold_output
    ]
    if len(entries) != 1 or _truthy_bool(entries[0].get("mute")):
        return False
    sources = entries[0].get("sources")
    expected = {
        channel: (gain_db, inverted) for channel, gain_db, inverted in mono_sum_sources()
    }
    if not isinstance(sources, list) or len(sources) != len(expected):
        return False
    for source in sources:
        if not isinstance(source, dict) or _truthy_bool(source.get("mute")):
            return False
        channel = source.get("channel")
        # `pop` also makes a duplicated feed fail: the second occurrence of a
        # channel is no longer expected.
        if type(channel) is not int or channel not in expected:
            return False
        gain_db, inverted = expected.pop(channel)
        if not _float_matches(source.get("gain"), gain_db):
            return False
        if _truthy_bool(source.get("inverted")) is not inverted:
            return False
    return not expected


def _flat_graph_allowed(
    contract: OutputContract,
    *,
    config_path: str | None,
    summary: dict[str, Any],
    program_bake_pipe: bool = False,
    hard_muted_outputs: frozenset[int] = frozenset(),
    program_dest_map: tuple[int, ...] | None = None,
    required_mono_fold: int | None = None,
    mono_fold_proved: bool = False,
) -> GraphSafety:
    # Program-bake exemption (Stage B): a flat program graph whose playback is a
    # File/pipe sink (the active-leader's camilla#1 bake, NOT a DAC) is safe
    # regardless of the saved speaker topology — no DAC is attached, so no driver
    # can be over-driven and the full-range-to-tweeter invariant cannot fire.
    # Narrow and additive: it keys strictly on the File-pipe playback, so an
    # ALSA-sink flat graph (the dangerous full-range-to-DAC direction) takes the
    # roleful-topology block below unchanged.
    if program_bake_pipe:
        return GraphSafety(
            classification=GRAPH_PROGRAM_BAKE_PIPE,
            allowed=True,
            config_path=config_path,
            camilla_classification=str(summary.get("classification") or "unknown"),
            playback_device=summary.get("playback_device"),
            playback_channels=summary.get("playback_channels"),
            issues=(),
            details={
                "contract_requires_roleful_graph": contract.requires_roleful_graph,
                "program_bake_pipe": True,
                "volume_limit_ok": bool(summary.get("volume_limit_ok")),
            },
        )
    issues: list[dict[str, str]] = []
    allowed = topology_allows_flat_dac_graph(contract)
    playback_channels = summary.get("playback_channels")
    full_range_outputs = flat_full_range_outputs(contract)
    # The invariant is "no emission on an output the topology does not claim".
    # A channel proved to be hard muted emits nothing, so it cannot reach an
    # undeclared output and must not be held against the topology. Everything
    # else is LIVE. `hard_muted_outputs` is proved structurally off the graph by
    # `_flat_hard_muted_outputs`, never taken from the renderer's intent — a
    # wide graph whose surplus channel is UNMUTED is refused here exactly as it
    # was before, under this same issue code and with the same message.
    #
    # How the live set is judged depends on what is known about the sink:
    #
    # * a RESOLVED `program_dest_map` — dest index IS physical output index, so
    #   the exact question can be asked: is any LIVE channel an output the
    #   topology does not claim? This is what makes muting the WRONG channel
    #   useless (a mono box that silences its claimed output still has a live
    #   channel landing on an undeclared one), and since the map also resolves a
    #   composite pairing it is what catches a wide composite graph whose program
    #   landed on the child-A pair instead of one output per child.
    # * UNDECIDED mapping on a graph WIDER than the program — refuse. Counting
    #   cannot speak here: the surplus dests are hard muted, so a graph feeding
    #   the wrong outputs has exactly as many live channels as the right one and
    #   the count is blind to the difference. #3219 widened the emitter and
    #   recorded that the refusal lands with the mapping; this is it.
    # * UNDECIDED mapping at the program's own width — count, as before: more
    #   live channels than assigned outputs means at least one lands somewhere
    #   undeclared under ANY injective mapping. The pre-existing rule, unchanged
    #   — it is why a 2-wide dual-Apple stereo box on outputs 0 and 2 is not
    #   refused.
    if isinstance(playback_channels, int) and not isinstance(playback_channels, bool):
        live_outputs = frozenset(range(playback_channels)) - hard_muted_outputs
    else:
        live_outputs = None
    if allowed and contract.topology_configured and live_outputs is not None:
        from jasper.sound.camilla_yaml import FLAT_PROGRAM_WIDTH

        code = "flat_full_range_graph_wider_than_topology"
        if program_dest_map is not None:
            undeclared = sorted(live_outputs - full_range_outputs)
            # The RECIPROCAL of "no emission on an undeclared output": a
            # declared output that nothing feeds. Only the program's dests (and
            # the fold, which sums onto one of them) carry program at all, so a
            # declared output outside that set gets the mixer's mute-floor feed
            # and the speaker is silent while every mute reads correct. #3219
            # recorded this hole against the mapping; the map is what makes it
            # decidable, so the refusal lands here with it.
            carries_program = frozenset(program_dest_map) | (
                frozenset() if required_mono_fold is None
                else frozenset({required_mono_fold})
            )
            silent = sorted(full_range_outputs - carries_program)
            if undeclared:
                detail = (
                    f"flat full-range graph emits on physical output(s) "
                    f"{', '.join(str(index) for index in undeclared)}, which the "
                    f"saved full-range topology does not assign"
                )
            elif silent:
                code = "flat_full_range_graph_declared_output_unfed"
                detail = (
                    f"flat full-range graph routes no program to declared "
                    f"physical output(s) "
                    f"{', '.join(str(index) for index in silent)}; the program "
                    f"reaches {', '.join(str(index) for index in sorted(carries_program))}"
                )
            else:
                detail = ""
        elif playback_channels > FLAT_PROGRAM_WIDTH:
            code = "flat_full_range_graph_mapping_undecided"
            detail = (
                f"flat full-range graph is {playback_channels} channels wide on "
                f"a sink whose program-to-output mapping is undecided, so no "
                f"live channel can be traced to a declared physical output"
            )
        else:
            over_wide = len(live_outputs) > len(full_range_outputs)
            detail = (
                f"flat full-range graph exposes {len(live_outputs)} output "
                f"channels, but saved full-range topology assigns only "
                f"{len(full_range_outputs)} physical output(s)"
            ) if over_wide else ""
        if detail:
            allowed = False
            issues.append(_issue("blocker", code, detail))
    if not allowed:
        if contract.classification == CONTRACT_UNCONFIGURED:
            issues.append(_issue(
                "blocker",
                "flat_full_range_graph_illegal_for_unconfigured_topology",
                "No speaker layout is configured; keep audio parked until a "
                "passive or active layout is saved.",
            ))
        elif contract.requires_roleful_graph:
            issues.append(_issue(
                "blocker",
                "flat_full_range_graph_illegal_for_roleful_topology",
                (
                    "Active speaker topology assigns "
                    f"{_protected_output_detail(contract)} to a roleful/protected role, "
                    "but Camilla is running a flat full-range graph. Normal playback "
                    "can send full-range signal to the protected driver. Load protected "
                    "active startup or disconnect/clear the topology."
                ),
            ))
        else:
            issues.append(_issue(
                "blocker",
                "flat_full_range_graph_requires_explicit_passive_layout",
                "A flat full-range graph requires a complete saved passive "
                "mono or stereo layout.",
            ))
    # The fold, re-proved. Muting the complement satisfies "no emission on an
    # undeclared output" but leaves a mono cabinet playing the program's LEFT
    # channel only — half the record, and quietly wrong rather than loudly
    # wrong, which is exactly the class a structural check must catch. The
    # renderer already refuses to emit an unfolded mono graph; this is the
    # checker's independent proof off the emitted YAML, so the single-owner
    # principle the mutes rest on holds for the fold too.
    #
    # BELOW the ladder above, and deliberately: that ladder explains why the
    # saved LAYOUT forbids a flat graph, and a box refused only for a missing
    # fold has a perfectly good layout. Refusing here instead of before it
    # keeps the operator from being sent to re-save a topology that is already
    # right. `required_mono_fold` is the RENDERER's own plan
    # (`_required_mono_fold_output`), so a topology the renderer would not fold
    # is never asked to.
    if required_mono_fold is not None and not mono_fold_proved:
        allowed = False
        issues.append(_issue(
            "blocker",
            "flat_full_range_graph_mono_fold_missing",
            (
                "Mono full-range topology assigns one physical output "
                f"({required_mono_fold}), but the graph's {_MASTER_GAIN_MIXER} "
                "mixer does not fold both program channels onto it at the "
                "clip-safe mono-sum gain; the speaker would play only the "
                "program's left channel."
            ),
        ))
    return GraphSafety(
        classification=GRAPH_FLAT_FULL_RANGE,
        allowed=allowed,
        config_path=config_path,
        camilla_classification=str(summary.get("classification") or "unknown"),
        playback_device=summary.get("playback_device"),
        playback_channels=summary.get("playback_channels"),
        issues=tuple(issues),
        details={
            "contract_requires_roleful_graph": contract.requires_roleful_graph,
            "volume_limit_ok": bool(summary.get("volume_limit_ok")),
            "hard_muted_outputs": sorted(hard_muted_outputs),
            "mono_fold_output": required_mono_fold,
        },
    )


ACTIVE_SPLIT_MIXER_PREFIX = "split_active_"


def _pipeline_mixer_names(payload: dict[str, Any]) -> list[str]:
    """The names of the ``Mixer`` pipeline steps in order (``Filter`` steps
    excluded).

    ``GraphView.pipeline_steps`` captures only ``Filter`` steps, so the
    channel-select / split mixer ORDER — which the driver-domain arm must prove —
    is read from the parsed payload here rather than from the shared view.
    """
    pipeline = payload.get("pipeline")
    if not isinstance(pipeline, list):
        return []
    names: list[str] = []
    for step in pipeline:
        if not isinstance(step, dict) or step.get("type") != "Mixer":
            continue
        name = step.get("name")
        if isinstance(name, str):
            names.append(name)
    return names


def _channel_select_precedes_split(mixer_names: list[str]) -> bool:
    """True iff a ``channel_select`` Mixer step runs strictly before the
    ``split_active_*`` Mixer step — the inter-speaker pick before the
    intra-speaker driver split. Fails closed (missing either -> ``False``)."""
    if _channel_select_mixer_name not in mixer_names:
        return False
    select_idx = mixer_names.index(_channel_select_mixer_name)
    split_idxs = [
        i for i, name in enumerate(mixer_names)
        if name.startswith(ACTIVE_SPLIT_MIXER_PREFIX)
    ]
    return bool(split_idxs) and select_idx < min(split_idxs)


def _program_domain_filter_step_names(view: GraphView) -> tuple[str, ...]:
    """Filter names wired to the stereo program bus ``[0, 1]``.

    A driver-domain follower has no program-domain Filter step at all: it mixes
    channel_select -> optional pair trim -> split_active, then filters physical
    driver outputs. So a Filter step on exactly channels [0, 1] is Layer B/C
    leaking onto the follower, except for the dedicated pair-balance trim.
    """
    names: list[str] = []
    for step in view.pipeline_steps:
        if step.channels == frozenset({0, 1}):
            names.extend(
                name for name in step.names if name != _DRIVER_DOMAIN_PAIR_TRIM
            )
    return tuple(names)


def _room_peq_filter_names(view: GraphView) -> tuple[str, ...]:
    return tuple(sorted(name for name in view.filters if name.startswith("room_peq")))


def _driver_domain_pair_trim_between_select_and_split(
    payload: dict[str, Any],
) -> bool:
    """Prove ``channel_select -> pair_balance_trim -> split_active_*`` order.

    ``GraphView`` intentionally stores only Filter steps, so this raw-pipeline
    check owns the mixed Mixer/Filter ordering proof for the optional pair trim.
    """
    pipeline = payload.get("pipeline")
    if not isinstance(pipeline, list):
        return False
    select_idx: int | None = None
    trim_idx: int | None = None
    split_idxs: list[int] = []
    for idx, raw_step in enumerate(pipeline):
        step = raw_step if isinstance(raw_step, dict) else {}
        step_type = step.get("type")
        if step_type == "Mixer":
            name = step.get("name")
            if name == _channel_select_mixer_name and select_idx is None:
                select_idx = idx
            if isinstance(name, str) and name.startswith(ACTIVE_SPLIT_MIXER_PREFIX):
                split_idxs.append(idx)
            continue
        if step_type != "Filter":
            continue
        names = step.get("names")
        if not isinstance(names, list) or _DRIVER_DOMAIN_PAIR_TRIM not in names:
            continue
        if trim_idx is not None:
            return False
        trim_idx = idx
    if select_idx is None or trim_idx is None or not split_idxs:
        return False
    return select_idx < trim_idx < min(split_idxs)


def _filter_step_channels(step: dict[str, Any]) -> set[int] | None:
    raw_channels = step.get("channels")
    if not isinstance(raw_channels, list) or any(
        isinstance(value, bool) for value in raw_channels
    ):
        return None
    try:
        return {int(value) for value in raw_channels}
    except (TypeError, ValueError):
        return None


def _exact_filter_step_channels(
    step: dict[str, Any], expected: set[int]
) -> bool:
    raw_channels = step.get("channels")
    return (
        isinstance(raw_channels, list)
        and len(raw_channels) == len(expected)
        and all(type(value) is int for value in raw_channels)
        and set(raw_channels) == expected
    )


def _strict_finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _post_split_filter_names(
    payload: dict[str, Any],
    *,
    channel: int,
) -> tuple[str, ...]:
    pipeline = payload.get("pipeline")
    if not isinstance(pipeline, list):
        return ()
    split_seen = False
    out: list[str] = []
    for raw_step in pipeline:
        step = raw_step if isinstance(raw_step, dict) else {}
        if step.get("type") == "Mixer":
            name = step.get("name")
            if isinstance(name, str) and name.startswith(ACTIVE_SPLIT_MIXER_PREFIX):
                split_seen = True
            continue
        if not split_seen or step.get("type") != "Filter":
            continue
        step_channels = _filter_step_channels(step)
        if step_channels is None or channel not in step_channels:
            continue
        raw_names = step.get("names")
        if not isinstance(raw_names, list):
            continue
        out.extend(
            name if isinstance(name, str) else "<invalid-filter-name>"
            for name in raw_names
        )
    return tuple(out)


def _pipeline_names_for_channels(
    payload: dict[str, Any],
    *,
    channels: set[int],
) -> tuple[str, ...]:
    pipeline = payload.get("pipeline")
    if not isinstance(pipeline, list):
        return ()
    out: list[str] = []
    for raw_step in pipeline:
        step = raw_step if isinstance(raw_step, dict) else {}
        if step.get("type") != "Filter":
            continue
        step_channels = _filter_step_channels(step)
        if step_channels is None:
            continue
        # A Camilla filter step may intentionally apply one role's baseline
        # chain to multiple outputs at once, for example both stereo woofers.
        # For per-output evidence we only need to prove that the requested
        # output is covered by the chain.
        if not channels.issubset(step_channels):
            continue
        out.extend(str(name) for name in step.get("names", []) if name is not None)
    return tuple(out)


def _unsafe_post_split_gains(payload: dict[str, Any]) -> tuple[str, ...]:
    """Gain filters after the active split must remain non-positive.

    Program-domain preference EQ can legitimately boost before the split because
    every driver limiter remains downstream. After the split, an added positive
    Gain could sit behind that limiter and defeat the active-output ceiling.
    """

    pipeline = payload.get("pipeline")
    if not isinstance(pipeline, list):
        return ()
    split_seen = False
    unsafe: set[str] = set()
    for raw_step in pipeline:
        step = raw_step if isinstance(raw_step, dict) else {}
        if step.get("type") == "Mixer":
            name = step.get("name")
            if isinstance(name, str) and name.startswith(ACTIVE_SPLIT_MIXER_PREFIX):
                split_seen = True
            continue
        if not split_seen or step.get("type") != "Filter":
            continue
        names = step.get("names")
        if not isinstance(names, list):
            continue
        for name in names:
            if not isinstance(name, str) or _filter_type(payload, name) != "Gain":
                continue
            gain = _strict_finite_number(_filter_params(payload, name).get("gain"))
            if gain is None or gain > 0.0:
                unsafe.add(name)
    return tuple(sorted(unsafe))


def _safe_commissioning_tail_filter(payload: dict[str, Any], name: str) -> bool:
    runtime_lane = name.startswith("as_commission_")
    output_mute = False
    if name.startswith("as_out") and name.endswith("_commission_mute"):
        index_s = name.removeprefix("as_out").removesuffix("_commission_mute")
        try:
            index = int(index_s)
        except ValueError:
            pass
        else:
            output_mute = name == _commission_mute_name(index)
    if not runtime_lane and not output_mute:
        return False
    filter_type = _filter_type(payload, name)
    params = _filter_params(payload, name)
    if filter_type == "Delay":
        delay_ms = _strict_finite_number(params.get("delay"))
        return (
            params.get("unit") == "ms"
            and delay_ms is not None
            and 0.0 <= delay_ms <= MAX_DSP_DELAY_US / 1000.0
        )
    if filter_type == "Gain":
        gain = _strict_finite_number(params.get("gain"))
        return (
            gain is not None
            and gain <= 0.0
            and type(params.get("inverted")) is bool
            and type(params.get("mute")) is bool
        )
    return False


def _post_limiter_tail_evidence(
    payload: dict[str, Any],
    *,
    channel: int,
    limiter_name: str,
) -> tuple[int, tuple[str, ...]]:
    """Count the post-split limiter and reject transforms placed behind it."""

    names = _post_split_filter_names(payload, channel=channel)
    limiter_count = names.count(limiter_name)
    unsafe: set[str] = set()
    if limiter_count:
        start = names.index(limiter_name) + 1
        for name in names[start:]:
            if name != limiter_name and not _safe_commissioning_tail_filter(
                payload, name
            ):
                unsafe.add(name)
    return limiter_count, tuple(sorted(unsafe))


def _post_split_delay_evidence(
    payload: dict[str, Any],
    *,
    channel: int,
) -> tuple[float, tuple[str, ...]]:
    """Return cumulative physical delay and malformed lanes for one output."""

    total_ms = 0.0
    invalid: set[str] = set()
    for name in _post_split_filter_names(payload, channel=channel):
        if _filter_type(payload, name) != "Delay":
            continue
        params = _filter_params(payload, name)
        delay_ms = _strict_finite_number(params.get("delay"))
        if (
            params.get("unit") != "ms"
            or delay_ms is None
            or delay_ms < 0.0
        ):
            invalid.add(name)
            continue
        total_ms += delay_ms
    return total_ms, tuple(sorted(invalid))


_WAY_COUNT_BY_MAIN_MODE = {
    "full_range_passive": 1,
    "active_2_way": 2,
    "active_3_way": 3,
}


def _crossover_directions(assignment: OutputAssignment) -> tuple[str, ...] | None:
    way_count = _WAY_COUNT_BY_MAIN_MODE.get(assignment.speaker_mode)
    if way_count is None:
        return None
    directions: list[str] = []
    for lower_role, upper_role in ADJACENT_PAIRS_BY_WAY[way_count]:
        if assignment.role == lower_role:
            directions.append("lowpass")
        if assignment.role == upper_role:
            directions.append("highpass")
    return tuple(directions) or None


def _crossover_filter_safe(
    payload: dict[str, Any],
    *,
    name: str,
    role: str,
    direction: str,
) -> bool:
    suffix = "lp" if direction == "lowpass" else "hp"
    params = _filter_params(payload, name)
    order = params.get("order")
    frequency = _strict_finite_number(params.get("freq"))
    minimum_frequency = (
        TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ
        if role == "tweeter" and direction == "highpass"
        else 0.0
    )
    return (
        name.startswith(f"as_{role}_")
        and name.endswith(f"_{suffix}")
        and _filter_type(payload, name) == "BiquadCombo"
        and params.get("type") == f"LinkwitzRiley{direction.title()}"
        and frequency is not None
        and frequency > 0.0
        and frequency >= minimum_frequency
        and not isinstance(order, bool)
        and isinstance(order, int)
        and order in SUPPORTED_LR_ORDERS
    )


def _bass_management_filter_safe(
    payload: dict[str, Any],
    *,
    name: str,
    direction: str,
) -> bool:
    params = _filter_params(payload, name)
    return (
        _filter_type(payload, name) == "BiquadCombo"
        and params.get("type") == f"LinkwitzRiley{direction.title()}"
        and SUB_CROSSOVER_HZ_LO
        <= (_strict_finite_number(params.get("freq")) or 0.0)
        <= SUB_CROSSOVER_HZ_HI
        and params.get("order") == SUB_CROSSOVER_ORDER
    )


def _baseline_gain_limiter_safe(
    payload: dict[str, Any],
    *,
    gain_name: str,
    limiter_name: str,
    exact_baseline_limiter: bool = False,
) -> bool:
    gain_params = _filter_params(payload, gain_name)
    gain = _strict_finite_number(gain_params.get("gain"))
    limiter_params = _filter_params(payload, limiter_name)
    clip_limit = _strict_finite_number(limiter_params.get("clip_limit"))
    return (
        _filter_type(payload, gain_name) == "Gain"
        and gain is not None
        and gain <= 0.0
        and type(gain_params.get("inverted")) is bool
        and gain_params.get("mute") is False
        and _filter_type(payload, limiter_name) == "Limiter"
        and clip_limit is not None
        and (
            clip_limit == BASELINE_LIMITER_CLIP_LIMIT_DB
            if exact_baseline_limiter
            else clip_limit <= 0.0
        )
        and limiter_params.get("soft_clip") is True
    )


# Float slack (dB) on the boost-vs-headroom proof. The emitter writes both
# numbers with 3-decimal formatting, so an exactly-absorbed boost can read a
# hair over its allowance after the YAML round-trip; this keeps a graph that
# is correct by construction from failing its own proof on the last digit.
_LINEARIZATION_BOOST_EPS_DB: float = 1e-3

#: The one NUMERIC refusal in this walk, named apart from the shape refusals.
#:
#: Public because two other seams key on it rather than re-deriving the
#: condition: ``safe_graph_for_current_topology`` refuses to fall silently past
#: an active graph that carries it (#2758's migration shape), and the deploy
#: transcript prints it. A shape refusal says the graph is not the emitter's;
#: this one says the graph IS the emitter's and its arithmetic no longer holds,
#: which is a different sentence with a different remedy (re-emit, not
#: re-commission).
LINEARIZATION_HEADROOM_UNPROVEN_CODE = "active_linearization_headroom_unproven"

#: Journal name for the same event. A grep contract, so a rename is visible as
#: one — this module says almost nothing on its own logger, and a numeric
#: refusal that leaves no trace is how a silent speaker gets diagnosed twice.
EVENT_LINEARIZATION_HEADROOM_UNPROVEN = (
    "active_speaker.linearization_headroom_unproven"
)


def _linearization_boost_allowance_db(payload: dict[str, Any]) -> float:
    """How much branch-chain peak THIS graph has already paid for.

    Since #1808 the quantity proved against this allowance is the branch
    chain's evaluated PEAK (``crossover ⊗ linearization ⊗ trim``), not the sum
    of the chain's positive filter gains — see
    :func:`_consume_linearization_chain`. The allowance itself, below, is
    unchanged: it is still what the emitter set aside, and the emitter now
    sets aside the peak plus ``branch_chain.HEADROOM_MARGIN_DB``, so a graph
    correct by construction proves with exactly that margin of slack.

    The magnitude of the program-domain ``active_baseline_headroom`` gain —
    the pre-split common attenuation the emitter folds baseline headroom,
    room-correction boost, and (since PR-L5) linearization boost into —
    **minus the contributors that are not linearization's**. A branch whose
    boosts total no more than what is left cannot drive the chain past unity,
    so the CamillaDSP 0 dB ceiling holds by arithmetic rather than by a policy
    number written down twice.

    Attributing the share matters, and reading the whole magnitude was wrong
    (adversarial review S1, reproduced): that gain also absorbs room-correction
    boost, so on a cut-only linearization sitting behind, say, 8 dB of room
    boost, a tampered +5 dB linearization filter "spent" headroom that was
    already committed to the room PEQs and the graph proved safe while the two
    together could clip. Room-PEQ boost is recoverable from the graph — the
    filters are named and their gains are readable — so it is subtracted here,
    exactly as the emitter added it.

    **Residual slack, stated rather than hidden**: ``output_trim_db`` (the
    household's manual headroom / loudness-match attenuation) is also folded
    into the same gain and is NOT recoverable from the graph. The emitter only
    ever adds it when preference EQ is present, so on a graph with no
    preference filters this allowance is exact; with preference EQ it is
    generous by at most that trim. Generous, never tight — the failure
    direction is a tamper spending the household's own trim as boost headroom,
    which is bounded and far narrower than the pre-fix behaviour.

    **One stated coincidence**: the emitter adds a CALLER-SUPPLIED
    ``baseline_headroom_db`` (validated 0..40), while this subtracts the module
    DEFAULT :data:`~jasper.active_speaker.camilla_yaml.BASELINE_HEADROOM_DB`.
    They agree only because every production emit path takes the default, which
    is 0.0 — pinned by a test rather than left to be discovered. A caller that
    passed a non-default value would make this allowance generous by exactly
    that amount (never tight, the same direction as the ``output_trim_db``
    slack above), and the pin is what would catch it.

    Returns 0.0 when the filter is absent or non-negative — which is the
    driver-domain (follower) graph, where the leader owns Layer B/C and no
    program-domain headroom exists. That graph therefore proves the ORIGINAL
    cut-only invariant, which is the correct fail-closed answer: a follower
    has nothing to absorb a boost with.
    """
    if _filter_type(payload, "active_baseline_headroom") != "Gain":
        return 0.0
    gain = _strict_finite_number(
        _filter_params(payload, "active_baseline_headroom").get("gain")
    )
    if gain is None or gain >= 0.0:
        return 0.0
    absorbed_db = -float(gain)
    filters = payload.get("filters")
    room_boost_db = 0.0
    if isinstance(filters, Mapping):
        for name in filters:
            if not isinstance(name, str) or not name.startswith("room_peq"):
                continue
            room_gain = _strict_finite_number(
                _filter_params(payload, name).get("gain")
            )
            if room_gain is not None and room_gain > 0.0:
                room_boost_db += float(room_gain)
    return max(0.0, absorbed_db - BASELINE_HEADROOM_DB - room_boost_db)


def _linearization_biquad(payload: dict[str, Any], name: str) -> dict[str, Any]:
    """One emitted linearization Biquad reduced to the plain
    ``{biquad_type, freq, q, gain}`` record
    :func:`jasper.active_speaker.branch_chain.chain_response` evaluates.

    Read straight off the graph text — this module never trusts a candidate's
    claim about what it emitted.
    """
    params = _filter_params(payload, name)
    return {
        "biquad_type": str(params.get("type") or ""),
        "freq": _strict_finite_number(params.get("freq")) or 0.0,
        "q": _strict_finite_number(params.get("q")) or 0.0,
        "gain": _strict_finite_number(params.get("gain")) or 0.0,
    }


def _linearization_chain_peak_db(
    payload: dict[str, Any],
    *,
    filters: Sequence[Mapping[str, Any]],
    crossovers: Sequence[tuple[str, str]],
    gain_name: str,
) -> tuple[float, float]:
    """The realized peak of this branch's emitted chain — ``(dB, Hz)``,
    re-derived from the graph, never from the candidate that produced it
    (#1808).

    ``crossover ⊗ linearization ⊗ trim``, the same three terms and the same
    :func:`jasper.active_speaker.branch_chain.branch_chain_peak` the
    emitter charges ``active_baseline_headroom`` with, so a graph that is
    correct by construction cannot fail its own proof on a modelling
    difference. The frequency rides along so a refusal can NAME where the
    chain peaks rather than only how high. Every input comes from the payload: the Linkwitz-Riley
    corner/order out of the named BiquadCombos this walk already validated,
    the biquad params out of the named linearization filters, and the trim out
    of the branch's baseline Gain.

    A trim that is absent or unreadable is treated as 0 dB (no credited
    attenuation), which over-states the peak — the safe direction for a proof.
    """
    from .branch_chain import CrossoverSection, branch_chain_peak

    sections: list[CrossoverSection] = []
    for direction, name in crossovers:
        params = _filter_params(payload, name)
        freq = _strict_finite_number(params.get("freq"))
        order = params.get("order")
        if freq is None or isinstance(order, bool) or not isinstance(order, int):
            continue
        sections.append(
            CrossoverSection(
                fc_hz=float(freq), order=int(order), highpass=direction == "highpass",
            )
        )
    trim_db = _strict_finite_number(_filter_params(payload, gain_name).get("gain"))
    return branch_chain_peak(
        filters,
        sections=tuple(sections),
        trim_db=min(0.0, float(trim_db)) if trim_db is not None else 0.0,
    )


def _linearization_filter_safe(
    payload: dict[str, Any],
    *,
    name: str,
    biquad_types: tuple[str, ...],
    max_gain_db: float,
) -> bool:
    """One named linearization Biquad proves its declared type is one of the
    allowed ``biquad_types`` for its slot, and that its gain is inside what
    this graph can carry -- the same posture
    ``linearization_fit.fit_driver_linearization`` enforces at fit time,
    re-proved independently here against the emitted graph. The shelf slot
    allows Highshelf OR Lowshelf (#1668 CD-horn backbone); the peak slot
    allows Peaking; the taper slot allows Highshelf.

    ``max_gain_db`` is the per-filter REALIZATION cap
    (``camilla_yaml.MAX_LINEARIZATION_BOOST_DB``), the same bound the fit
    engine re-proves on every emitted filter: past it an RBJ biquad's
    Q-dependent transition stops being a faithful realization of the shape
    that was asked for. Cuts are unconditionally safe (any negative gain
    passes, as they always did).

    It is NOT the clipping rail. That is the whole chain's business — a boost
    is safe when the branch it sits in cannot drive the program above the
    attenuation the graph provably applies ahead of the split — and it is
    proved once per branch in :func:`_consume_linearization_chain`, over the
    evaluated ``crossover ⊗ linearization ⊗ trim`` peak (#1808). Per-filter
    this check used to carry the allowance too, which would refuse an
    ordinary legitimate graph under the peak rule: a +6 dB boost sitting
    inside its own crossover's stopband costs nothing and would be rejected
    against a charge of zero."""

    if _filter_type(payload, name) != "Biquad":
        return False
    params = _filter_params(payload, name)
    if str(params.get("type") or "") not in biquad_types:
        return False
    gain = _strict_finite_number(params.get("gain"))
    return gain is not None and gain <= max_gain_db


def _consume_linearization_chain(
    chain: tuple[str, ...],
    cursor: int,
    payload: dict[str, Any],
    role: str,
    *,
    crossovers: Sequence[tuple[str, str]] = (),
    notes: list[dict[str, str]] | None = None,
) -> tuple[int, bool]:
    """Advance ``cursor`` past a well-formed, provably-safe Layer-1a
    linearization run (#1668) for ``role``: an optional named leading shelf
    (Highshelf rising-slope OR Lowshelf CD-horn backbone), then 0..N named
    peaking filters, then an optional named trailing Highshelf taper — in the
    emitter's own naming convention
    (``camilla_yaml.driver_linearization_shelf_name`` /
    ``driver_linearization_peak_name`` / ``driver_linearization_taper_name``,
    re-exported via ``graph_evidence``).

    SELF-PROVING from the graph text alone -- unlike bass-extension (a
    business decision an external profile/candidate declares, so its
    presence/shape must be threaded in as evidence), a linearization
    filter's full shape (which role, shelf/peak/taper slot, how many peaks) is
    entirely recoverable from its own name + params. So no
    ``linearization_summary`` parameter needs threading through this
    module's public entry points (``classify_camilla_graph`` /
    ``classify_bass_extension_graph``) or their ~8 external callers the way
    ``bass_profile_summary`` does — this stays a purely-local addition to
    ``_baseline_output_chain``.

    ``notes`` is an optional sink for the ONE refusal a caller cannot
    reconstruct from a bare ``False``: the headroom proof is a NUMERIC
    comparison, and its failure used to surface only as the caller's
    ``active_output_driver_chain_unrecognized`` — "does not use the exact
    ordered emitter chain", when the order was right and the arithmetic was
    not. An issue appended here carries the peak, the allowance and the
    FREQUENCY, so the operator reads what failed instead of inferring it. It
    is a sink rather than a return value because every other refusal in this
    walk is honestly a shape refusal and needs no words.

    Returns ``(new_cursor, ok)``. ``ok`` is False iff a recognized
    linearization-named filter proves UNSAFE (wrong Biquad subtype for its
    slot, or gain outside what the graph can carry) — fail closed, exactly
    like every other named-filter proof in this module. The shelf slot accepts
    Highshelf or Lowshelf; the taper slot accepts only Highshelf. A name at
    ``cursor`` that does not match the linearization naming convention is not
    an error: zero filters are consumed, and the ordinary tail check the caller
    runs next decides whether what remains (unshifted) is a legal chain.

    **Boost accounting (PR-L5, re-derived at the realized peak by #1808).**
    Cuts are unconditionally safe. A boost is safe only if the graph
    attenuates the program by at least as much ahead of the split, so this
    walk EVALUATES the branch chain it just proved the shape of — the
    crossover BiquadCombos, the linearization biquads, and the branch's own
    baseline Gain — and proves its peak against
    :func:`_linearization_boost_allowance_db`.

    Until 2026-07-28 the walk summed the chain's positive gains instead. That
    sum is an upper bound on the peak, so it never permitted an unsafe graph;
    what it did was force the emitter to CHARGE the same loose bound, and the
    2026-07-28 JTS3 profile paid 22.458 dB of program attenuation for a branch
    whose realized peak was +4.00 dB (#1808). Emitter and prover have to agree
    about one number, so both moved to the exact one — the same
    ``branch_chain.branch_chain_peak_db``, over the same three terms.

    That is not a weakening: the peak IS the quantity "how much does this
    branch put above unity", and the sum was a proxy for it. It does newly
    permit a boost that its own crossover fully removes (a filter deep in a
    branch's stopband now costs nothing and proves nothing), which is
    physically correct — such a filter cannot clip — and is separately
    prevented from being GENERATED by the fit-band bound (#1809). Per-filter,
    the realization cap still binds. On a graph with no program-domain
    headroom the allowance is 0.0, and a cut-only chain's peak is <= 0, so
    that graph proves exactly what it proved before.
    """

    index = cursor
    allowance_db = _linearization_boost_allowance_db(payload)
    emitted: list[dict[str, Any]] = []

    shelf_name = _linearization_shelf_name(role)
    if index < len(chain) and chain[index] == shelf_name:
        if not _linearization_filter_safe(
            payload, name=shelf_name, biquad_types=("Highshelf", "Lowshelf"),
            max_gain_db=MAX_LINEARIZATION_BOOST_DB,
        ):
            return index, False
        emitted.append(_linearization_biquad(payload, shelf_name))
        index += 1
    peak_number = 1
    while index < len(chain):
        peak_name = _linearization_peak_name(role, peak_number)
        if chain[index] != peak_name:
            break
        if not _linearization_filter_safe(
            payload, name=peak_name, biquad_types=("Peaking",),
            max_gain_db=MAX_LINEARIZATION_BOOST_DB,
        ):
            return index, False
        emitted.append(_linearization_biquad(payload, peak_name))
        index += 1
        peak_number += 1
    taper_name = _linearization_taper_name(role)
    if index < len(chain) and chain[index] == taper_name:
        if not _linearization_filter_safe(
            payload, name=taper_name, biquad_types=("Highshelf",),
            max_gain_db=MAX_LINEARIZATION_BOOST_DB,
        ):
            return index, False
        emitted.append(_linearization_biquad(payload, taper_name))
        index += 1
    # A chain with no positive gain cannot exceed unity through a
    # Linkwitz-Riley section and a non-positive trim, so the ordinary cut-only
    # graph is proved without evaluating anything — and without this module
    # importing numpy, which it otherwise does not (see branch_chain).
    if not any(float(entry["gain"]) > 0.0 for entry in emitted):
        return index, True
    peak_db, peak_hz = _linearization_chain_peak_db(
        payload,
        filters=emitted,
        crossovers=crossovers,
        gain_name=_baseline_gain_name(role),
    )
    if peak_db > allowance_db + _LINEARIZATION_BOOST_EPS_DB:
        detail = (
            f"{role} linearization chain peaks {peak_db:.4f} dB at "
            f"{peak_hz:.1f} Hz, past the {allowance_db:.4f} dB this graph set "
            "aside for it ahead of the split; the chain's ORDER is correct and "
            "the headroom arithmetic is what failed"
        )
        log_event(
            logger,
            EVENT_LINEARIZATION_HEADROOM_UNPROVEN,
            level=logging.WARNING,
            role=role,
            peak_db=round(peak_db, 4),
            peak_hz=round(peak_hz, 1),
            allowance_db=round(allowance_db, 4),
        )
        if notes is not None:
            notes.append(_issue(
                "blocker", LINEARIZATION_HEADROOM_UNPROVEN_CODE, detail,
            ))
        return index, False
    return index, True


def _baseline_output_chain(
    payload: dict[str, Any],
    *,
    assignment: OutputAssignment,
    channel: int,
    bass_management_highpass: bool,
    bass_extension: bool = False,
    notes: list[dict[str, str]] | None = None,
) -> tuple[tuple[str, str], ...] | None:
    """Prove the exact emitter-owned chain before the canonical limiter.

    ``notes`` is handed straight to :func:`_consume_linearization_chain`, the
    one refusal here that is arithmetic rather than shape — see its docstring.
    Every other ``None`` this returns genuinely means "not the emitter's
    chain", which the caller's own issue already says."""

    names = _post_split_filter_names(payload, channel=channel)
    if assignment.role == "subwoofer":
        expected = (
            _sub_lowpass_name(),
            *(("bass_ext_lt", "bass_ext_subsonic") if bass_extension else ()),
            _sub_baseline_gain_name(),
            _sub_baseline_limiter_name(),
        )
        return (
            ()
            if (
                names[: len(expected)] == expected
                and _bass_management_filter_safe(
                    payload,
                    name=_sub_lowpass_name(),
                    direction="lowpass",
                )
                and _baseline_gain_limiter_safe(
                    payload,
                    gain_name=_sub_baseline_gain_name(),
                    limiter_name=_sub_baseline_limiter_name(),
                    exact_baseline_limiter=bass_extension,
                )
            )
            else None
        )

    limiter_name = _baseline_limiter_name(assignment.role)
    if names.count(limiter_name) != 1:
        return None
    limiter_index = names.index(limiter_name)
    chain = names[: limiter_index + 1]
    cursor = 0
    if bass_management_highpass:
        bass_name = _bass_management_hp_name(assignment.role)
        if (
            not chain
            or chain[0] != bass_name
            or not _bass_management_filter_safe(
                payload,
                name=bass_name,
                direction="highpass",
            )
        ):
            return None
        cursor += 1
    directions = _crossover_directions(assignment)
    if directions is None:
        return None
    crossovers: list[tuple[str, str]] = []
    for direction in directions:
        if cursor >= len(chain):
            return None
        name = chain[cursor]
        if not _crossover_filter_safe(
            payload,
            name=name,
            role=assignment.role,
            direction=direction,
        ):
            return None
        crossovers.append((direction, name))
        cursor += 1
    # Layer-1a driver linearization (#1668 PR-D): immediately after the
    # crossover HP/LP, before bass-extension — mirrors the bass-extension
    # slot above but is SELF-PROVING from the graph text alone (see
    # _consume_linearization_chain's docstring for why no external "what
    # linearization SHOULD be here" evidence needs threading through this
    # module's callers the way bass_extension's boolean does).
    cursor, linearization_ok = _consume_linearization_chain(
        chain, cursor, payload, assignment.role,
        crossovers=tuple(crossovers), notes=notes,
    )
    if not linearization_ok:
        return None
    if bass_extension:
        if tuple(chain[cursor : cursor + 2]) != (
            "bass_ext_lt",
            "bass_ext_subsonic",
        ):
            return None
        cursor += 2
    expected_tail = (
        _driver_delay_name(assignment.role),
        _baseline_gain_name(assignment.role),
        limiter_name,
    )
    delay_params = _filter_params(payload, expected_tail[0])
    delay_ms = _strict_finite_number(delay_params.get("delay"))
    if (
        chain[cursor:] != expected_tail
        or _filter_type(payload, expected_tail[0]) != "Delay"
        or delay_params.get("unit") != "ms"
        or delay_ms is None
        or not 0.0 <= delay_ms <= MAX_DSP_DELAY_US / 1000.0
        or not _baseline_gain_limiter_safe(
            payload,
            gain_name=expected_tail[1],
            limiter_name=limiter_name,
            exact_baseline_limiter=bass_extension,
        )
    ):
        return None
    return tuple(crossovers)


def _commissioning_output_chain(
    payload: dict[str, Any],
    *,
    assignment: OutputAssignment,
    channel: int,
    bass_management_highpass: bool,
) -> tuple[tuple[str, str], ...] | None:
    """Prove one exact commissioning chain through its per-output mute."""

    names = _post_split_filter_names(payload, channel=channel)
    mute_name = _commission_mute_name(channel)
    mute_params = _filter_params(payload, mute_name)
    mute_gain = _strict_finite_number(mute_params.get("gain"))
    mute_safe = (
        _filter_type(payload, mute_name) == "Gain"
        and mute_gain is not None
        and mute_gain <= 0.0
        and type(mute_params.get("inverted")) is bool
        and type(mute_params.get("mute")) is bool
    )
    if assignment.role == "subwoofer":
        limiter_name = _sub_startup_limiter_name()
        expected = (_sub_lowpass_name(), limiter_name, mute_name)
        limiter = _filter_params(payload, limiter_name)
        clip_limit = _strict_finite_number(limiter.get("clip_limit"))
        return (
            ()
            if (
                names == expected
                and mute_safe
                and _bass_management_filter_safe(
                    payload,
                    name=_sub_lowpass_name(),
                    direction="lowpass",
                )
                and _filter_type(payload, limiter_name) == "Limiter"
                and clip_limit is not None
                and clip_limit <= 0.0
                and limiter.get("soft_clip") is True
            )
            else None
        )

    cursor = 0
    if bass_management_highpass:
        bass_name = _bass_management_hp_name(assignment.role)
        if (
            not names
            or names[0] != bass_name
            or not _bass_management_filter_safe(
                payload,
                name=bass_name,
                direction="highpass",
            )
        ):
            return None
        cursor += 1
    protective_name = protective_tweeter_hp_name(assignment.role)
    if cursor < len(names) and names[cursor] == protective_name:
        protective = _filter_params(payload, protective_name)
        protective_order = protective.get("order")
        if not (
            _filter_type(payload, protective_name) == "BiquadCombo"
            and protective.get("type") == "LinkwitzRileyHighpass"
            and (
                _strict_finite_number(protective.get("freq")) or 0.0
            ) >= TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ
            and not isinstance(protective_order, bool)
            and isinstance(protective_order, int)
            and protective_order in SUPPORTED_LR_ORDERS
        ):
            return None
        cursor += 1
    directions = _crossover_directions(assignment)
    if directions is None:
        return None
    crossovers: list[tuple[str, str]] = []
    for direction in directions:
        if cursor >= len(names):
            return None
        name = names[cursor]
        if not _crossover_filter_safe(
            payload,
            name=name,
            role=assignment.role,
            direction=direction,
        ):
            return None
        crossovers.append((direction, name))
        cursor += 1
    delay_name = _driver_delay_name(assignment.role)
    limiter_name = driver_limiter_name(assignment.role)
    expected_tail = (delay_name, limiter_name, mute_name)
    delay = _filter_params(payload, delay_name)
    delay_ms = _strict_finite_number(delay.get("delay"))
    limiter = _filter_params(payload, limiter_name)
    clip_limit = _strict_finite_number(limiter.get("clip_limit"))
    if (
        names[cursor:] != expected_tail
        or not mute_safe
        or _filter_type(payload, delay_name) != "Delay"
        or delay.get("unit") != "ms"
        or delay_ms is None
        or not 0.0 <= delay_ms <= MAX_DSP_DELAY_US / 1000.0
        or _filter_type(payload, limiter_name) != "Limiter"
        or clip_limit is None
        or clip_limit > 0.0
        or limiter.get("soft_clip") is not True
    ):
        return None
    return tuple(crossovers)


def _canonical_chain_grouped(
    payload: dict[str, Any],
    *,
    expected_channels: set[int],
    expected_names: tuple[str, ...],
) -> bool:
    pipeline = payload.get("pipeline")
    if not isinstance(pipeline, list):
        return False
    split_seen = False
    matches = 0
    for raw_step in pipeline:
        step = raw_step if isinstance(raw_step, dict) else {}
        if step.get("type") == "Mixer":
            name = step.get("name")
            if isinstance(name, str) and name.startswith(ACTIVE_SPLIT_MIXER_PREFIX):
                split_seen = True
            continue
        if not split_seen or step.get("type") != "Filter":
            continue
        raw_names = step.get("names")
        if not isinstance(raw_names, list):
            continue
        names = tuple(name for name in raw_names if isinstance(name, str))
        if (
            _exact_filter_step_channels(step, expected_channels)
            and names == expected_names
        ):
            matches += 1
    return matches == 1


def _crossover_pair_matches(
    payload: dict[str, Any], lower_name: str, upper_name: str
) -> bool:
    lower = _filter_params(payload, lower_name)
    upper = _filter_params(payload, upper_name)
    return (
        _strict_finite_number(lower.get("freq"))
        == _strict_finite_number(upper.get("freq"))
        and lower.get("order") == upper.get("order")
    )


def _mismatched_crossover_pairs(
    payload: dict[str, Any],
    crossovers_by_role: dict[str, tuple[tuple[str, str], ...]],
    way_counts: set[int],
) -> tuple[tuple[str, str], ...]:
    mismatched: list[tuple[str, str]] = []
    for way_count in sorted(way_counts):
        for lower_role, upper_role in ADJACENT_PAIRS_BY_WAY.get(way_count, ()):
            lower_name = next(
                (
                    name
                    for direction, name in crossovers_by_role.get(lower_role, ())
                    if direction == "lowpass"
                ),
                None,
            )
            upper_name = next(
                (
                    name
                    for direction, name in crossovers_by_role.get(upper_role, ())
                    if direction == "highpass"
                ),
                None,
            )
            if (
                lower_name is None
                or upper_name is None
                or not _crossover_pair_matches(payload, lower_name, upper_name)
            ):
                mismatched.append((lower_role, upper_role))
    return tuple(mismatched)


def _driver_domain_pair_trim_safe(
    payload: dict[str, Any],
    view: GraphView,
) -> bool:
    """Optional pair-balance trim must be a non-positive Gain on the stereo bus."""
    present = (
        _DRIVER_DOMAIN_PAIR_TRIM in view.filters
        or any(_DRIVER_DOMAIN_PAIR_TRIM in step.names for step in view.pipeline_steps)
    )
    if not present:
        return True
    gain = _float_value(_filter_params(payload, _DRIVER_DOMAIN_PAIR_TRIM).get("gain"))
    return (
        _filter_type(payload, _DRIVER_DOMAIN_PAIR_TRIM) == "Gain"
        and gain is not None
        and gain <= 0.0
        and pipeline_contains_chain(
            view,
            channels={0, 1},
            required_names=(_DRIVER_DOMAIN_PAIR_TRIM,),
        )
        and _driver_domain_pair_trim_between_select_and_split(payload)
    )


def _commission_mute_states(view: GraphView) -> dict[int, bool]:
    """Map each ``as_out{N}_commission_mute`` filter's output index to its
    ``mute`` boolean, read from the shared view's parsed filters.

    The ``as_out{N}_commission_mute`` name pattern is runtime_contract-specific
    (``graph_safety``'s predicates take a single ``mute_name``, never a pattern),
    so the scan stays here — but it now reads the already-parsed
    ``GraphView.filters`` instead of re-walking the raw config dict.
    """
    out: dict[int, bool] = {}
    for name, fdef in view.filters.items():
        if not name.startswith("as_out") or not name.endswith("_commission_mute"):
            continue
        index_s = name.removeprefix("as_out").removesuffix("_commission_mute")
        try:
            index = int(index_s)
        except ValueError:
            continue
        out[index] = bool(fdef.params.get("mute"))
    return out


def _baseline_commissioning_pair(
    contract: OutputContract,
    unmuted_outputs: set[int],
) -> tuple[str, tuple[str, str]] | None:
    """Infer one exact adjacent pair in one active speaker group."""

    if len(unmuted_outputs) != 2:
        return None
    by_output = _assignment_by_output(contract)
    assignments = [by_output.get(index) for index in sorted(unmuted_outputs)]
    if any(item is None for item in assignments):
        return None
    exact = [item for item in assignments if item is not None]
    group_ids = {item.speaker_group_id for item in exact}
    modes = {item.speaker_mode for item in exact}
    if len(group_ids) != 1 or len(modes) != 1:
        return None
    mode = next(iter(modes))
    way_count = _WAY_COUNT_BY_MAIN_MODE.get(mode)
    if way_count not in {2, 3}:
        return None
    roles = {item.role for item in exact}
    pair = next(
        (
            candidate
            for candidate in ADJACENT_PAIRS_BY_WAY[way_count]
            if set(candidate) == roles
        ),
        None,
    )
    if pair is None:
        return None
    return next(iter(group_ids)), pair


def _baseline_commissioning_isolation_issues(
    payload: dict[str, Any],
    contract: OutputContract,
    *,
    graph_indexes: set[int],
    mutes: dict[int, bool],
    unmuted_outputs: set[int],
) -> tuple[list[dict[str, str]], tuple[str, tuple[str, str]] | None]:
    """Independently prove the runtime-owned final per-output mute tail."""

    issues: list[dict[str, str]] = []
    if set(mutes) != graph_indexes:
        issues.append(_issue(
            "blocker",
            "active_baseline_commissioning_mute_set_invalid",
            (
                "summed commissioning baseline must define exactly one mute "
                "filter for every graph output"
            ),
        ))
    filters = payload.get("filters")
    pipeline = payload.get("pipeline")
    expected_steps: list[dict[str, Any]] = []
    for index in sorted(graph_indexes):
        name = _commission_mute_name(index)
        is_audible = index in unmuted_outputs
        expected_filter = {
            "type": "Gain",
            "parameters": {
                "gain": 0.0 if is_audible else STARTUP_MUTE_GAIN_DB,
                "inverted": False,
                "mute": not is_audible,
            },
        }
        definition = filters.get(name) if isinstance(filters, dict) else None
        if definition != expected_filter:
            issues.append(_issue(
                "blocker",
                "active_baseline_commissioning_mute_invalid",
                (
                    "summed commissioning output mute is not the exact canonical "
                    f"state for DAC output {index + 1}"
                ),
            ))
        expected_steps.append(
            {"type": "Filter", "channels": [index], "names": [name]}
        )
        if not _canonical_chain_grouped(
            payload,
            expected_channels={index},
            expected_names=(name,),
        ):
            issues.append(_issue(
                "blocker",
                "active_baseline_commissioning_mute_step_invalid",
                (
                    "summed commissioning must wire one exact output mute step "
                    f"for DAC output {index + 1}"
                ),
            ))
    tail = (
        pipeline[-len(expected_steps):]
        if isinstance(pipeline, list) and expected_steps
        else []
    )
    if tail != expected_steps:
        issues.append(_issue(
            "blocker",
            "active_baseline_commissioning_mute_tail_invalid",
            (
                "summed commissioning output mutes must be the final ordered "
                "pipeline tail"
            ),
        ))
    pair = _baseline_commissioning_pair(contract, unmuted_outputs)
    if pair is None:
        issues.append(_issue(
            "blocker",
            "active_baseline_commissioning_target_invalid",
            (
                "summed commissioning may unmute exactly two adjacent roles "
                "within one active speaker group"
            ),
        ))
    return issues, pair


def _assignment_by_output(contract: OutputContract) -> dict[int, OutputAssignment]:
    out: dict[int, OutputAssignment] = {}
    for item in contract.assignments:
        if item.physical_output_index is not None and item.roleful:
            out[item.physical_output_index] = item
    return out


def _required_roleful_indexes(contract: OutputContract) -> set[int]:
    return {
        int(item.physical_output_index)
        for item in contract.roleful_assignments
        if item.physical_output_index is not None
    }


# The lowest (woofer / full-range) driver role per main mode — the driver that
# carries the bass-management high-pass. Mirrors profile.LOWEST_DRIVER_ROLE_BY_WAY
# but keyed by the topology's speaker_mode (the verifier re-derives independently
# of the emitter's preset, so it does not import that table).
_LOWEST_MAIN_ROLE_BY_MODE = {
    "full_range_passive": "full_range",
    "active_2_way": "woofer",
    "active_3_way": "woofer",
}


def _subwoofer_output_indexes(contract: OutputContract) -> set[int]:
    """Physical output indices the saved topology assigns to a subwoofer role."""
    return {
        int(item.physical_output_index)
        for item in contract.assignments
        if item.role == "subwoofer" and item.physical_output_index is not None
    }


def _mains_lowest_driver_indexes(contract: OutputContract) -> set[int]:
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
        if _LOWEST_MAIN_ROLE_BY_MODE.get(item.speaker_mode) == item.role:
            out.add(int(item.physical_output_index))
    return out


def _active_graph_evidence(
    text: str,
    contract: OutputContract,
    summary: dict[str, Any],
    bass_profile_summary: Mapping[str, Any] | None,
) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    # Parse the text ONCE. `payload` gives the two distinct parse-error codes
    # this module's callers branch on (camilla_yaml_unparseable vs
    # camilla_yaml_not_object — which the shared view collapses to
    # parsed_ok=False) AND backs the baseline path's raw-dict filter accessors +
    # subset pipeline-name lookup. The normalised view for the predicate calls
    # below is built from that SAME dict via view_from_yaml_dict (list-only, like
    # the candidate dialect — not the sugar-reading view_from_camilla_dict), so
    # the same text is never yaml.safe_load-ed twice.
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        issues.append(_issue(
            "blocker",
            "camilla_yaml_unparseable",
            f"could not parse CamillaDSP YAML: {type(exc).__name__}",
        ))
        return {"issues": issues, "safe": False}
    if not isinstance(payload, dict):
        issues.append(_issue(
            "blocker",
            "camilla_yaml_not_object",
            "CamillaDSP YAML did not parse to an object",
        ))
        return {"issues": issues, "safe": False}
    view = view_from_yaml_dict(payload)

    required_indexes = _required_roleful_indexes(contract)
    by_output = _assignment_by_output(contract)
    required_count = max(required_indexes) + 1 if required_indexes else 0
    split = summary.get("active_split") if isinstance(summary.get("active_split"), dict) else {}
    split_channels = split.get("mixer_output_channels")
    if not required_indexes:
        issues.append(_issue(
            "blocker",
            "active_graph_without_roleful_topology",
            "active-speaker graph is loaded but saved topology has no roleful outputs",
        ))
    if split_channels != required_count:
        issues.append(_issue(
            "blocker",
            "active_graph_output_count_mismatch",
            (
                f"active graph exposes {split_channels or 'unknown'} output channels; "
                f"saved roleful topology requires {required_count}"
            ),
        ))
    unsafe_output_gains = _unsafe_post_split_gains(payload)
    if unsafe_output_gains:
        issues.append(_issue(
            "blocker",
            "active_output_gain_positive",
            (
                "active graph has a positive or malformed Gain after the driver "
                "split: " + ", ".join(unsafe_output_gains)
            ),
        ))

    mutes = _commission_mute_states(view)
    graph_indexes = (
        set(range(split_channels))
        if isinstance(split_channels, int) and split_channels >= 0
        else set(required_indexes)
    )
    missing_mutes = sorted(index for index in required_indexes if index not in mutes)
    if missing_mutes:
        source = str(summary.get("source") or "")
        if source not in _BASELINE_LIKE_SOURCES:
            issues.append(_issue(
                "blocker",
                "active_graph_missing_commission_mutes",
                "active graph is missing per-output mute filters for DAC outputs "
                + ", ".join(str(index + 1) for index in missing_mutes),
            ))
    weak_mutes = sorted(
        index for index in required_indexes
        if mutes.get(index) is True
        and not filter_param_matches(
            view,
            _commission_mute_name(index),
            filter_type="Gain",
            params={"gain": STARTUP_MUTE_GAIN_DB},
        )
    )
    if weak_mutes:
        issues.append(_issue(
            "blocker",
            "active_graph_commission_mute_not_hard_mute",
            "active graph mute filters are not at the expected hard-mute floor for DAC outputs "
            + ", ".join(str(index + 1) for index in weak_mutes),
        ))

    unwired_mutes = sorted(
        index for index in required_indexes
        if not pipeline_contains_chain(
            view,
            channels={index},
            required_names=(_commission_mute_name(index),),
        )
    )
    if unwired_mutes:
        source = str(summary.get("source") or "")
        if source not in _BASELINE_LIKE_SOURCES:
            issues.append(_issue(
                "blocker",
                "active_graph_unwired_commission_mutes",
                "active graph does not wire per-output mutes for DAC outputs "
                + ", ".join(str(index + 1) for index in unwired_mutes),
            ))

    source = str(summary.get("source") or "")
    is_baseline = source == ACTIVE_BASELINE_SOURCE
    is_driver_domain = source == ACTIVE_DRIVER_DOMAIN_SOURCE
    is_baseline_commissioning = is_baseline and bool(mutes)
    if is_driver_domain and mutes:
        issues.append(_issue(
            "blocker",
            "active_driver_domain_commission_mutes_present",
            "driver-domain baseline must not carry runtime commissioning mutes",
        ))
    # Both baseline-shaped graphs retain the same protective per-driver chain;
    # the primary baseline may additionally carry the exact runtime-owned
    # summed-isolation tail proved below. They otherwise differ only in the
    # pre-split prefix, branched inside the `is_baseline_like` block.
    is_baseline_like = is_baseline or is_driver_domain
    bass_owner_channels: set[int] = set()
    if is_baseline_like:
        if bass_profile_summary is None:
            issues.append(_issue(
                "blocker",
                "bass_extension_evidence_missing",
                "baseline-shaped graph requires explicit bass-extension profile evidence",
            ))
        else:
            bass_evidence = bass_extension_block_valid(view, bass_profile_summary)
            if not bass_evidence.valid:
                issues.append(_issue(
                    "blocker",
                    bass_evidence.reason or "bass_extension_block_invalid",
                    "baseline-shaped graph does not match its evaluated bass-extension profile",
                ))
            if bass_evidence.expected:
                bass_owner_channels = set(bass_evidence.reference_channels)
    mixer_names = _pipeline_mixer_names(payload)
    active_way_counts = {
        way_count
        for item in contract.assignments
        if (way_count := _WAY_COUNT_BY_MAIN_MODE.get(item.speaker_mode)) is not None
    }
    expected_split = (
        f"split_active_{next(iter(active_way_counts))}way"
        if len(active_way_counts) == 1
        else None
    )
    expected_mixers = (
        (_channel_select_mixer_name, expected_split)
        if is_driver_domain and expected_split is not None
        else ((expected_split,) if expected_split is not None else ())
    )
    if tuple(mixer_names) != expected_mixers:
        issues.append(_issue(
            "blocker",
            "active_graph_mixer_sequence_invalid",
            (
                "active graph must retain the exact emitter mixer sequence with "
                "one active split and no post-split mixer"
            ),
        ))
    unmuted_outputs = (
        set(graph_indexes)
        if is_baseline_like and not is_baseline_commissioning
        else {
            index for index in graph_indexes
            if index in mutes and mutes[index] is False
        }
    )
    muted_outputs = {
        index for index in required_indexes
        if index in mutes and mutes[index] is True
    }
    all_muted = bool(required_indexes) and muted_outputs == required_indexes
    baseline_commissioning_pair: tuple[str, tuple[str, str]] | None = None
    if is_baseline_commissioning:
        isolation_issues, baseline_commissioning_pair = (
            _baseline_commissioning_isolation_issues(
                payload,
                contract,
                graph_indexes=graph_indexes,
                mutes=mutes,
                unmuted_outputs=unmuted_outputs,
            )
        )
        issues.extend(isolation_issues)

    tweeter_outputs = {
        int(item.physical_output_index)
        for item in contract.protected_assignments
        if item.physical_output_index is not None and item.role == "tweeter"
    }
    if tweeter_outputs and not is_baseline_like:
        if not tweeter_guard_present(
            view,
            channels=tweeter_outputs,
            hp_name=protective_tweeter_hp_name("tweeter"),
            limiter_name=driver_limiter_name("tweeter"),
            limiter_clip_ceiling_db=STARTUP_LIMITER_CLIP_LIMIT_DB,
        ):
            issues.append(_issue(
                "blocker",
                "active_graph_tweeter_guard_missing",
                (
                    "active graph does not prove tweeter outputs are wrapped by "
                    "the protective high-pass and limiter"
                ),
            ))

    # All physical outputs the saved topology assigns (roleful drivers + sub +
    # full-range passive mains). A bass-managed passive main is a full_range
    # output — legitimately unmuted/routed but NOT roleful — so the unknown-output
    # guards below must treat it as known, not as an unexpected leak.
    known_indexes = {
        int(item.physical_output_index)
        for item in contract.assignments
        if item.physical_output_index is not None
    }
    sub_outputs = _subwoofer_output_indexes(contract)
    mains_low_outputs = _mains_lowest_driver_indexes(contract)
    unmuted_roles = {
        by_output[index].role
        for index in unmuted_outputs
        if index in by_output
    }
    unknown_unmuted = sorted(index for index in unmuted_outputs if index not in known_indexes)
    if unknown_unmuted:
        issues.append(_issue(
            "blocker",
            "active_graph_unmutes_unknown_outputs",
            "active graph unmutes outputs not assigned by the saved topology: "
            + ", ".join(str(index + 1) for index in unknown_unmuted),
        ))
    if len(unmuted_roles) > 1 and not is_baseline_like:
        issues.append(_issue(
            "blocker",
            "active_graph_unmutes_multiple_roles",
            "guarded commissioning may unmute only one driver role at a time",
        ))
    if unmuted_outputs & tweeter_outputs and any(
        issue["code"] == "active_graph_tweeter_guard_missing" for issue in issues
    ):
        issues.append(_issue(
            "blocker",
            "active_graph_unprotected_tweeter_audible",
            "active graph unmutes a tweeter output without proving software protection",
        ))

    # Local-subwoofer audible-protection guard (commissioning/startup) — the
    # non-baseline analogue of the baseline sub re-proof below. A sub output that
    # is UNMUTED (audible) MUST be band-limited (LR4 low-pass) + excursion-limited;
    # a full-range feed to a powered sub is exactly the corrupted/tampered-statefile
    # hazard the re-proof exists to catch (the honest emitter keeps the sub muted in
    # the commissioning sequence, but restore_active_camilla_solo loads a
    # guarded_commissioning graph off disk). The baseline path proves the sub
    # separately — with its non-positive gain — inside is_baseline_like; the
    # commissioning sub lane has no gain filter, so only LP + limiter are provable.
    # Gated not-baseline-like so a baseline graph (different limiter name) is never
    # tripped by this check. Mirrors the tweeter audible guard above.
    if not is_baseline_like:
        for index in sorted(unmuted_outputs & sub_outputs):
            if not sub_audible_guard_present(
                view,
                channels={index},
                lowpass_name=_sub_lowpass_name(),
                # The corner ceiling is load-bearing: a sub LOW-pass at a high
                # corner (e.g. 20 kHz) is full-range to a bass driver, so cap it
                # at the legal sub-crossover ceiling. The baseline class bounds
                # the corner via bass_management_corner_matched instead.
                lowpass_freq_ceiling_hz=SUB_CROSSOVER_HZ_HI,
                limiter_name=_sub_startup_limiter_name(),
                limiter_clip_ceiling_db=STARTUP_LIMITER_CLIP_LIMIT_DB,
            ):
                issues.append(_issue(
                    "blocker",
                    "active_graph_unprotected_sub_audible",
                    (
                        "active graph unmutes a subwoofer output without proving "
                        "the band-limit + excursion limiter on DAC output "
                        f"{index + 1}"
                    ),
                ))

    if not is_baseline_like:
        commissioning_crossovers: dict[str, tuple[tuple[str, str], ...]] = {}
        for index in sorted(required_indexes):
            assignment = by_output.get(index)
            if assignment is None:
                continue
            role = assignment.role
            crossovers = _commissioning_output_chain(
                payload,
                assignment=assignment,
                channel=index,
                bass_management_highpass=(
                    contract.subwoofer_present and index in mains_low_outputs
                ),
            )
            if crossovers is None:
                issues.append(_issue(
                    "blocker",
                    "active_commissioning_chain_unrecognized",
                    (
                        "active graph does not use the exact ordered commissioning "
                        f"chain through its mute on DAC output {index + 1} ({role})"
                    ),
                ))
                continue
            prior = commissioning_crossovers.setdefault(role, crossovers)
            if prior != crossovers:
                issues.append(_issue(
                    "blocker",
                    "active_commissioning_chain_unrecognized",
                    f"active graph uses inconsistent {role} commissioning chains",
                ))
            role_channels = {
                output for output, item in by_output.items() if item.role == role
            }
            post_split_names = _post_split_filter_names(payload, channel=index)
            role_chain_names = post_split_names[:-1]
            if index == min(role_channels) and not _canonical_chain_grouped(
                payload,
                expected_channels=role_channels,
                expected_names=role_chain_names,
            ):
                issues.append(_issue(
                    "blocker",
                    "active_commissioning_chain_not_grouped",
                    (
                        f"active graph must wire one exact grouped {role} "
                        "commissioning chain across its current outputs"
                    ),
                ))
            if not _canonical_chain_grouped(
                payload,
                expected_channels={index},
                expected_names=(_commission_mute_name(index),),
            ):
                issues.append(_issue(
                    "blocker",
                    "active_commissioning_mute_step_invalid",
                    (
                        "active graph must end each physical output with one exact "
                        f"commission mute step on DAC output {index + 1}"
                    ),
                ))
        for lower_role, upper_role in _mismatched_crossover_pairs(
            payload,
            commissioning_crossovers,
            active_way_counts,
        ):
            issues.append(_issue(
                "blocker",
                "active_commissioning_crossover_pair_mismatch",
                (
                    f"active graph {lower_role}/{upper_role} commissioning "
                    "crossovers must share one finite corner and LR order"
                ),
            ))

    if is_baseline_like:
        if is_baseline:
            # Program-domain prefix: the shared headroom gain rides channels
            # [0, 1] before the split and must be non-positive.
            if not pipeline_contains_chain(
                view,
                channels={0, 1},
                required_names=("active_baseline_headroom",),
            ):
                issues.append(_issue(
                    "blocker",
                    "active_baseline_headroom_unwired",
                    "active baseline graph does not wire the shared headroom filter",
                ))
            headroom = _float_value(
                _filter_params(payload, "active_baseline_headroom").get("gain")
            )
            if headroom is None or headroom > 0.0:
                issues.append(_issue(
                    "blocker",
                    "active_baseline_headroom_invalid",
                    "active baseline headroom gain is missing or positive",
                ))
        else:
            # Driver-domain (follower) prefix: the leader baked Layer B/C, so
            # this graph carries NO program-domain prefix. Prove (a) the
            # inter-speaker channel-select runs strictly before the
            # intra-speaker split, and (b) no program-domain headroom gain
            # leaked in (its presence would mean an un-relocated Layer B/C on
            # the follower). channel-select is a Mixer step, read from the
            # parsed pipeline order rather than the Filter-only GraphView.
            if _channel_select_mixer_name not in mixer_names:
                issues.append(_issue(
                    "blocker",
                    "active_driver_domain_channel_select_missing",
                    "driver-domain graph does not wire the channel-select mixer",
                ))
            elif not _channel_select_precedes_split(mixer_names):
                issues.append(_issue(
                    "blocker",
                    "active_driver_domain_channel_select_after_split",
                    "driver-domain channel-select must run before the driver split",
                ))
            if "active_baseline_headroom" in view.filters:
                issues.append(_issue(
                    "blocker",
                    "active_driver_domain_program_prefix_present",
                    (
                        "driver-domain graph carries a program-domain headroom "
                        "filter (the leader owns Layer B/C, not the follower)"
                    ),
                ))
            room_peqs = _room_peq_filter_names(view)
            if room_peqs:
                issues.append(_issue(
                    "blocker",
                    "active_driver_domain_room_peq_present",
                    (
                        "driver-domain graph carries room-correction PEQ filters "
                        "(the leader owns Layer B, not the follower): "
                        + ", ".join(room_peqs)
                    ),
                ))
            program_step_names = _program_domain_filter_step_names(view)
            if program_step_names:
                issues.append(_issue(
                    "blocker",
                    "active_driver_domain_program_filter_step_present",
                    (
                        "driver-domain graph wires program-domain filters on "
                        "channels [0, 1] (the leader owns Layer B/C, not the "
                        "follower): "
                        + ", ".join(program_step_names)
                    ),
                ))
            if not _driver_domain_pair_trim_safe(payload, view):
                issues.append(_issue(
                    "blocker",
                    "active_driver_domain_pair_trim_invalid",
                    (
                        "driver-domain pair-balance trim must be a non-positive "
                        "Gain wired to the selected stereo bus before the driver split"
                    ),
                ))
        unknown_baseline_outputs = sorted(graph_indexes - known_indexes)
        if unknown_baseline_outputs:
            issues.append(_issue(
                "blocker",
                "active_baseline_routes_unknown_outputs",
                "active baseline routes outputs not assigned by the saved topology: "
                + ", ".join(str(index + 1) for index in unknown_baseline_outputs),
            ))
        # Local-subwoofer bass-management re-proof. A sub topology DEMANDS the sub
        # guard (the sub output is band-limited + excursion-limited + gain<=0) AND
        # the complementary mains high-pass on every main's lowest driver — the two
        # halves of one crossover. A half-present crossover (sub LP without the
        # mains HP, or a sub output missing its low-pass) is fail-closed UNSAFE.
        if contract.subwoofer_present:
            for index in sorted(sub_outputs):
                if not sub_guard_present(
                    view,
                    channels={index},
                    lowpass_name=_sub_lowpass_name(),
                    gain_name=_sub_baseline_gain_name(),
                    limiter_name=_sub_baseline_limiter_name(),
                    limiter_clip_ceiling_db=BASELINE_LIMITER_CLIP_LIMIT_DB,
                ):
                    issues.append(_issue(
                        "blocker",
                        "active_baseline_sub_guard_missing",
                        (
                            "active baseline subwoofer output is not band-limited, "
                            "excursion-limited, and non-positive-gain on DAC output "
                            f"{index + 1}"
                        ),
                    ))
            if not mains_low_outputs:
                issues.append(_issue(
                    "blocker",
                    "active_baseline_bass_mgmt_mains_missing",
                    (
                        "saved topology has a subwoofer but no main lowest-driver "
                        "output to carry the complementary bass-management high-pass"
                    ),
                ))
            else:
                # The emitter folds the bass-management HP into the lowest driver's
                # role-grouped Filter step (one step targets all of that role's
                # outputs — both stereo sides), so the HP is proven once against
                # the whole lowest-driver output set. Active mains: woofer
                # (roleful). Passive mains: full_range (not roleful, keyed by the
                # full_range role). A mixed-mode set would split, but main mode is
                # uniform in the supported topologies.
                low_role = next(
                    (
                        by_output[index].role
                        for index in sorted(mains_low_outputs)
                        if index in by_output
                    ),
                    "full_range",
                )
                bass_highpass_name = _bass_management_hp_name(low_role)
                if (
                    not mains_highpass_present(
                        view,
                        channels=mains_low_outputs,
                        highpass_name=bass_highpass_name,
                    )
                    or not _bass_management_filter_safe(
                        payload,
                        name=bass_highpass_name,
                        direction="highpass",
                    )
                ):
                    issues.append(_issue(
                        "blocker",
                        "active_baseline_bass_mgmt_highpass_missing",
                        (
                            "active baseline main lowest-driver outputs are missing "
                            "the complementary bass-management high-pass on DAC "
                            "outputs "
                            + ", ".join(
                                str(index + 1) for index in sorted(mains_low_outputs)
                            )
                            + f" ({low_role})"
                        ),
                    ))
                elif not bass_management_corner_matched(
                    view,
                    lowpass_name=_sub_lowpass_name(),
                    highpass_name=_bass_management_hp_name(low_role),
                ):
                    # Both halves exist, but at DIFFERENT corners — not two halves
                    # of one crossover. A split crossover (e.g. an 80 Hz mains HP
                    # under a 1000 Hz sub LP) leaves the sub reproducing midrange or
                    # a mid-band hole. The emitter drives both from one Fc, so this
                    # only fires on a corrupted/tampered statefile — fail closed.
                    issues.append(_issue(
                        "blocker",
                        "active_baseline_bass_mgmt_corner_split",
                        (
                            "active baseline sub low-pass and mains bass-management "
                            "high-pass are at different corners — not two halves of "
                            "one crossover (the crossover Fc has been split)"
                        ),
                    ))
        crossovers_by_role: dict[str, tuple[tuple[str, str], ...]] = {}
        for index in sorted(required_indexes):
            assignment = by_output.get(index)
            if assignment is None:
                continue
            role = assignment.role
            # The sub output's protection is proven by sub_guard_present above
            # (its gain/limiter names are sub-specific, not role-derived). Its
            # post-limiter tail still needs the same fail-closed check as a main.
            limiter_name = (
                _sub_baseline_limiter_name()
                if role == "subwoofer"
                else _baseline_limiter_name(role)
            )
            chain_notes: list[dict[str, str]] = []
            crossovers = _baseline_output_chain(
                payload,
                assignment=assignment,
                channel=index,
                bass_management_highpass=(
                    contract.subwoofer_present and index in mains_low_outputs
                ),
                bass_extension=index in bass_owner_channels,
                notes=chain_notes,
            )
            if crossovers is None:
                # The NUMERIC refusal reports itself, with the peak, the
                # allowance and the frequency; only fall back to the shape
                # sentence when the shape is genuinely what failed. Saying both
                # would put "does not use the exact ordered emitter chain" next
                # to an arithmetic failure and send the reader after the wrong
                # defect — which is exactly what this walk did before #2758.
                if chain_notes:
                    issues.extend(chain_notes)
                else:
                    issues.append(_issue(
                        "blocker",
                        "active_output_driver_chain_unrecognized",
                        (
                            "active graph does not use the exact ordered emitter "
                            f"chain on DAC output {index + 1} ({role})"
                        ),
                    ))
            else:
                prior = crossovers_by_role.setdefault(role, crossovers)
                if prior != crossovers:
                    issues.append(_issue(
                        "blocker",
                        "active_output_driver_chain_unrecognized",
                        f"active graph uses inconsistent {role} crossover chains",
                    ))
                role_channels = {
                    output
                    for output, item in by_output.items()
                    if item.role == role
                }
                post_split_names = _post_split_filter_names(payload, channel=index)
                limiter_index = post_split_names.index(limiter_name)
                expected_names = post_split_names[: limiter_index + 1]
                if index == min(role_channels) and not _canonical_chain_grouped(
                    payload,
                    expected_channels=role_channels,
                    expected_names=expected_names,
                ):
                    issues.append(_issue(
                        "blocker",
                        "active_output_driver_chain_not_grouped",
                        (
                            f"active graph must wire one exact grouped {role} "
                            "driver chain across its current outputs"
                        ),
                    ))
            limiter_count, unsafe_tail = _post_limiter_tail_evidence(
                payload,
                channel=index,
                limiter_name=limiter_name,
            )
            if limiter_count != 1:
                issues.append(_issue(
                    "blocker",
                    "active_output_limiter_order_invalid",
                    (
                        "active graph must wire exactly one canonical limiter "
                        f"after the active split on DAC output {index + 1}; "
                        f"found {limiter_count}"
                    ),
                ))
            if unsafe_tail:
                issues.append(_issue(
                    "blocker",
                    "active_output_post_limiter_filter_unsafe",
                    (
                        "active graph has an unapproved filter after the canonical "
                        f"limiter on DAC output {index + 1}: "
                        + ", ".join(unsafe_tail)
                    ),
                ))
            total_delay_ms, invalid_delays = _post_split_delay_evidence(
                payload,
                channel=index,
            )
            if invalid_delays:
                issues.append(_issue(
                    "blocker",
                    "active_output_delay_invalid",
                    (
                        "active graph has a malformed post-split delay on DAC "
                        f"output {index + 1}: " + ", ".join(invalid_delays)
                    ),
                ))
            maximum_delay_ms = MAX_DSP_DELAY_US / 1000.0
            if total_delay_ms > maximum_delay_ms:
                issues.append(_issue(
                    "blocker",
                    "active_output_delay_ceiling_exceeded",
                    (
                        "active graph cumulative post-split delay exceeds the "
                        f"{maximum_delay_ms:g} ms ceiling on DAC output "
                        f"{index + 1}: {total_delay_ms:g} ms"
                    ),
                ))
            if role == "subwoofer":
                continue
            gain_name = _baseline_gain_name(role)
            names = _pipeline_names_for_channels(payload, channels={index})
            if limiter_name not in names or gain_name not in names:
                issues.append(_issue(
                    "blocker",
                    "active_baseline_driver_chain_missing",
                    (
                        "active baseline graph does not wire gain and limiter "
                        f"filters for DAC output {index + 1} ({role})"
                    ),
                ))
            limiter_params = _filter_params(payload, limiter_name)
            limiter_clip = _float_value(limiter_params.get("clip_limit"))
            if (
                _filter_type(payload, limiter_name) != "Limiter"
                or limiter_clip is None
                or not math.isfinite(limiter_clip)
                or limiter_clip > 0.0
                or not _truthy_bool(limiter_params.get("soft_clip"))
            ):
                issues.append(_issue(
                    "blocker",
                    "active_baseline_limiter_invalid",
                    (
                        "active baseline limiter is missing or unsafe for "
                        f"DAC output {index + 1} ({role})"
                    ),
                ))
            gain = _float_value(_filter_params(payload, gain_name).get("gain"))
            if gain is None or not math.isfinite(gain) or gain > 0.0:
                issues.append(_issue(
                    "blocker",
                    "active_baseline_gain_positive",
                    (
                        "active baseline driver gain is missing or positive for "
                        f"DAC output {index + 1} ({role})"
                    ),
                ))
            if index in tweeter_outputs:
                highpass_names = [
                    name for name in names
                    if _filter_type(payload, name) == "BiquadCombo"
                    and str(_filter_params(payload, name).get("type") or "")
                    == "LinkwitzRileyHighpass"
                    and (_float_value(_filter_params(payload, name).get("freq")) or 0.0)
                    > 0.0
                ]
                if not highpass_names:
                    issues.append(_issue(
                        "blocker",
                        "active_baseline_tweeter_highpass_missing",
                        (
                            "active baseline tweeter output is missing a "
                            f"wired high-pass filter on DAC output {index + 1}"
                        ),
                    ))
        for lower_role, upper_role in _mismatched_crossover_pairs(
            payload,
            crossovers_by_role,
            active_way_counts,
        ):
            issues.append(_issue(
                "blocker",
                "active_output_crossover_pair_mismatch",
                (
                    f"active graph {lower_role}/{upper_role} low-pass and "
                    "high-pass must share one finite corner and LR order"
                ),
            ))

    return {
        "safe": not issues,
        "issues": issues,
        "required_outputs": sorted(required_indexes),
        "unmuted_outputs": sorted(unmuted_outputs),
        "muted_outputs": sorted(muted_outputs),
        "all_muted": all_muted,
        "baseline_candidate": is_baseline and not is_baseline_commissioning,
        "baseline_commissioning_candidate": is_baseline_commissioning,
        "baseline_commissioning_group": (
            baseline_commissioning_pair[0]
            if baseline_commissioning_pair is not None
            else None
        ),
        "baseline_commissioning_roles": (
            list(baseline_commissioning_pair[1])
            if baseline_commissioning_pair is not None
            else []
        ),
        "driver_domain_candidate": is_driver_domain,
        "unmuted_roles": sorted(unmuted_roles),
        "tweeter_outputs": sorted(tweeter_outputs),
        "subwoofer_present": contract.subwoofer_present,
        "subwoofer_outputs": sorted(sub_outputs),
        "mains_bass_mgmt_outputs": sorted(mains_low_outputs),
        "split_channels": split_channels,
    }


def _staged_path(staged_config: dict[str, Any] | None) -> str | None:
    config = staged_config.get("config") if isinstance(staged_config, dict) else None
    if not isinstance(config, dict):
        return None
    raw = config.get("path")
    return str(raw) if isinstance(raw, str) and raw.strip() else None


def _staged_matches_topology(
    staged_config: dict[str, Any] | None,
    topology: OutputTopology,
) -> bool:
    if not isinstance(staged_config, dict) or staged_config.get("status") != "staged":
        return False
    staged_topology = staged_config.get("topology")
    staged_hardware = staged_config.get("hardware")
    if not isinstance(staged_topology, dict) or not isinstance(staged_hardware, dict):
        return False
    return all((
        staged_topology.get("topology_id") == topology.topology_id,
        staged_hardware.get("device_id") == topology.hardware.device_id,
        staged_hardware.get("card_id") == topology.hardware.card_id,
        staged_hardware.get("physical_output_count")
        == topology.hardware.physical_output_count,
        staged_hardware.get("clock_domain_id") == topology.hardware.clock_domain_id,
        target_assignment_signature(staged_target_signature(staged_config))
        == target_assignment_signature(topology_target_signature(topology)),
    ))


def _active_graph_allowed(
    text: str,
    topology: OutputTopology,
    contract: OutputContract,
    *,
    config_path: str | None,
    summary: dict[str, Any],
    staged_config: dict[str, Any] | None,
    bass_profile_summary: Mapping[str, Any] | None,
) -> GraphSafety:
    evidence = _active_graph_evidence(
        text, contract, summary, bass_profile_summary
    )
    issues = list(evidence.get("issues") or [])
    classification = GRAPH_UNSAFE
    if evidence.get("safe"):
        if evidence.get("driver_domain_candidate"):
            classification = GRAPH_DRIVER_DOMAIN_BASELINE
        elif evidence.get("baseline_candidate"):
            classification = GRAPH_APPROVED_ACTIVE_RUNTIME
        elif evidence.get("all_muted"):
            classification = GRAPH_ALL_MUTED_ACTIVE_STARTUP
        elif evidence.get("unmuted_outputs"):
            classification = GRAPH_GUARDED_COMMISSIONING
        else:
            classification = GRAPH_APPROVED_ACTIVE_RUNTIME

    staged_path = _staged_path(staged_config)
    staged_match = _staged_matches_topology(staged_config, topology)
    staged_guard_ready = (
        software_guard_ready_for_startup(topology, staged_config)
        if isinstance(staged_config, dict)
        else False
    )
    staged_dependent = staged_config is not None and classification in {
        GRAPH_ALL_MUTED_ACTIVE_STARTUP,
        GRAPH_GUARDED_COMMISSIONING,
    }
    if staged_dependent:
        if not staged_path or not config_path:
            issues.append(_issue(
                "blocker",
                "active_staged_metadata_missing",
                "guarded active graphs require a staged locator and graph path",
            ))
        elif not _path_matches(config_path, staged_path):
            issues.append(_issue(
                "blocker",
                "active_staged_locator_mismatch",
                "guarded active graph path does not match staged metadata",
            ))
        if not staged_match:
            issues.append(_issue(
                "blocker",
                "active_staged_metadata_mismatch",
                "guarded active graph no longer matches saved topology metadata",
            ))
        if not staged_guard_ready:
            issues.append(_issue(
                "blocker",
                "active_staged_guard_not_ready",
                "staged active metadata does not prove software guard readiness",
            ))

    allowed = classification in {
        GRAPH_ALL_MUTED_ACTIVE_STARTUP,
        GRAPH_GUARDED_COMMISSIONING,
        GRAPH_APPROVED_ACTIVE_RUNTIME,
        GRAPH_DRIVER_DOMAIN_BASELINE,
    } and not issues
    return GraphSafety(
        classification=classification if allowed else GRAPH_UNSAFE,
        allowed=allowed,
        config_path=config_path,
        camilla_classification=str(summary.get("classification") or "unknown"),
        playback_device=summary.get("playback_device"),
        playback_channels=summary.get("playback_channels"),
        issues=tuple(issues),
        details={
            **{k: v for k, v in evidence.items() if k not in {"issues", "safe"}},
            "staged_metadata_matches_topology": staged_match,
            "staged_guard_ready": staged_guard_ready,
        },
    )


def _required_output_width(contract: OutputContract) -> int:
    """The narrowest playback width that reaches every assigned physical output."""

    indexes = [
        assignment.physical_output_index
        for assignment in contract.assignments
        if assignment.physical_output_index is not None
    ]
    return max(indexes) + 1 if indexes else 0


def _parked_pipeline_is_exhaustive(payload: dict[str, Any], width: int) -> bool:
    """True iff the pipeline is EXACTLY the parked shape and nothing more.

    Whitelist, not blacklist: one leading ``Mixer``, then ``width`` ``Filter``
    steps, step *i* naming exactly channel *i* and exactly that channel's mute
    filter. Any surplus step, surplus name, missing step, reorder, or unexpected
    step type fails. Reading the raw pipeline (not ``GraphView``) is deliberate —
    ``GraphView`` keeps only ``Filter`` steps, so a ``Mixer``/``Dither``/
    ``Processor`` appended after the mutes would be invisible to it.
    """

    if width < 1:
        return False
    raw_steps = payload.get("pipeline")
    if not isinstance(raw_steps, list) or len(raw_steps) != width + 1:
        return False
    head, *tail = raw_steps
    if not isinstance(head, dict) or head.get("type") != "Mixer":
        return False
    for index, step in enumerate(tail):
        if not isinstance(step, dict) or step.get("type") != "Filter":
            return False
        if step.get("channels") != [index]:
            return False
        if step.get("names") != [_commission_mute_name(index)]:
            return False
    return True


def _parked_graph_allowed(
    text: str,
    contract: OutputContract,
    *,
    config_path: str | None,
    summary: dict[str, Any],
) -> GraphSafety:
    """Prove, independently of the emitter, that a PARKED graph is all-muted.

    A parked graph is accepted because this function CHECKS that it is silent —
    never because verification is skipped for a trusted filename or source
    marker. Four structural facts, all read off the parsed graph:

    1. ``devices.playback.type`` is ``File``. No DAC is attached, so no driver
       can be over-driven whatever the topology says — the same load-bearing key
       ``_playback_is_program_bake_pipe`` uses for the program-bake exemption.
    2. The pipeline is **exhaustively** the parked shape: one leading ``Mixer``
       step, then exactly ``width`` ``Filter`` steps, step *i* targeting channel
       *i* alone with ``names`` equal to exactly that channel's mute filter.
       Nothing else may appear — no extra step, no extra name inside a step, no
       reordering.
    3. Every playback channel's mute is a real hard mute — a ``Gain`` at
       ``STARTUP_MUTE_GAIN_DB`` with ``mute: true`` — proved by the same
       ``output_hard_muted_and_wired`` primitive the staged startup graph's
       crash-recovery invariant uses.
    4. The playback width covers every physical output the saved topology
       assigns, so no declared driver sits outside the muted set.

    **Why fact 2 must be exhaustive, stated exactly.** Fact 1 alone bounds the
    damage (a File sink reaches no driver), but it is NOT a substitute for fact
    3: a graph could be repointed at a DAC by a later edit while the pipeline
    stayed generous. Earlier revisions of this checker only required that a hard
    mute be *present somewhere* in each channel's chain, which the review panel
    falsified three ways — a ``+240 dB`` ``Gain`` appended as a fourth pipeline
    step, the same gain injected into an existing mute step's ``names`` list
    (CamillaDSP applies a step's filters in order, so a gain after the mute
    re-amplifies), and a ``Dither`` step appended (which *generates* signal into
    a muted channel). All three now fail: anything that is not byte-for-byte the
    parked shape is refused, so "muted" cannot be undone by addition.

    Fails closed on every unmet fact and on an unparseable graph.
    """

    issues: list[dict[str, str]] = []
    try:
        payload = yaml.safe_load(text)
    except (RecursionError, UnicodeError, ValueError, yaml.YAMLError):
        payload = None
    if not isinstance(payload, dict):
        return GraphSafety(
            classification=GRAPH_UNSAFE,
            allowed=False,
            config_path=config_path,
            camilla_classification=str(summary.get("classification") or "unknown"),
            playback_device=summary.get("playback_device"),
            playback_channels=summary.get("playback_channels"),
            issues=(
                _issue(
                    "blocker",
                    "parked_graph_unparseable",
                    "parked active-speaker graph is not a YAML object",
                ),
            ),
        )

    devices = payload.get("devices")
    playback = devices.get("playback") if isinstance(devices, dict) else None
    if not isinstance(playback, dict) or playback.get("type") != "File":
        issues.append(_issue(
            "blocker",
            "parked_graph_sink_not_file",
            "parked graph must write to a File sink, never to a DAC",
        ))

    raw_width = summary.get("playback_channels")
    width = (
        int(raw_width)
        if isinstance(raw_width, int) and not isinstance(raw_width, bool)
        else 0
    )

    if not _parked_pipeline_is_exhaustive(payload, width):
        issues.append(_issue(
            "blocker",
            "parked_graph_pipeline_shape",
            (
                "parked graph pipeline must be exactly one leading Mixer "
                "followed by one mute-only Filter step per output"
            ),
        ))

    required = _required_output_width(contract)
    if width < 1:
        issues.append(_issue(
            "blocker",
            "parked_graph_width_unknown",
            "parked graph does not declare a playback channel count",
        ))
    elif width < required:
        issues.append(_issue(
            "blocker",
            "parked_graph_width_too_narrow",
            (
                f"parked graph drives {width} outputs but the saved topology "
                f"assigns {required}"
            ),
        ))

    view = view_from_yaml_dict(payload)
    unmuted = [
        index
        for index in range(width)
        if not output_hard_muted_and_wired(
            view,
            index,
            mute_name=_commission_mute_name(index),
            mute_gain_db=STARTUP_MUTE_GAIN_DB,
        )
    ]
    if unmuted:
        issues.append(_issue(
            "blocker",
            "parked_graph_output_not_muted",
            (
                "parked graph leaves outputs without a wired hard mute: "
                + ", ".join(str(index) for index in unmuted)
            ),
        ))

    allowed = not issues
    return GraphSafety(
        classification=GRAPH_PARKED_ALL_MUTED if allowed else GRAPH_UNSAFE,
        allowed=allowed,
        config_path=config_path,
        camilla_classification=str(summary.get("classification") or "unknown"),
        playback_device=summary.get("playback_device"),
        playback_channels=summary.get("playback_channels"),
        issues=tuple(issues),
        details={
            "parked": allowed,
            "muted_outputs": width - len(unmuted),
            "required_outputs": required,
        },
    )


def classify_camilla_graph(
    config_path: str | Path | None = None,
    topology: OutputTopology | None = None,
    *,
    text: str | None = None,
    staged_config: dict[str, Any] | None = None,
    bass_profile_summary: Mapping[str, Any] | None = None,
) -> GraphSafety:
    """Return whether a CamillaDSP graph is legal for the saved topology."""

    topology = topology or load_output_topology_strict()
    contract = classify_output_contract(topology)
    # The two issue sources are kept APART because they answer different
    # questions, and the tail below gates the PARKED verdict on only one of
    # them (#2145). ``topology_issues`` describe the saved speaker LAYOUT (a
    # half-assigned mid-edit draft); ``graph_issues`` describe the CamillaDSP
    # config TEXT in hand (a missing volume_limit). Merged, they were
    # indistinguishable, which is how a topology blocker came to refuse a graph
    # it cannot make unsafe.
    topology_issues: list[dict[str, str]] = list(contract.issues)
    graph_issues: list[dict[str, str]] = []
    path_s = str(config_path) if config_path is not None else None
    if text is None:
        return GraphSafety(
            classification=GRAPH_UNKNOWN,
            allowed=False,
            config_path=path_s,
            issues=tuple(topology_issues) or (
                _issue("blocker", "camilla_graph_missing", "no CamillaDSP graph was provided"),
            ),
        )

    summary = classify_camilla_config_text(text)
    for issue in summary.get("issues", []):
        if isinstance(issue, dict):
            graph_issues.append(_issue(
                str(issue.get("severity") or "blocker"),
                str(issue.get("code") or "camilla_config_issue"),
                str(issue.get("message") or issue.get("code") or "CamillaDSP issue"),
            ))

    camilla_class = str(summary.get("classification") or "unknown")
    path_name = Path(path_s).name if path_s else ""
    is_flat = (
        camilla_class in {
            "jts_outputd_stereo",
            "jts_legacy_stereo",
            "jts_generated_stereo",
            # The active-leader camilla#1 program bake is also a flat (no-Layer-A)
            # program graph; it reaches the flat path so the File-sink exemption
            # in _flat_graph_allowed can clear it regardless of topology.
            CAMILLA_CLASS_PROGRAM_BAKE,
        }
        or path_name in {"outputd-cutover.yml", "v1.yml"}
    )
    if is_flat:
        # Detect the File/pipe playback ONCE here (this scope has the config
        # text); _flat_graph_allowed stays text-free. The exemption keys strictly
        # on the File-pipe sink, so an ALSA-sink flat graph stays subject to the
        # roleful-topology block.
        program_bake_pipe = _playback_is_program_bake_pipe(text)
        # Which playback channels this graph provably cannot emit on, read off
        # the graph here (same text-free split as program_bake_pipe above) so
        # _flat_graph_allowed can ask "does it emit anywhere undeclared?"
        # instead of the width proxy it used to. The mono fold is the same
        # split once more: the topology says whether one is owed, the text says
        # whether it is there.
        required_mono_fold = _required_mono_fold_output(
            topology, playback_channels=summary.get("playback_channels")
        )
        graph = _flat_graph_allowed(
            contract,
            config_path=path_s,
            summary=summary,
            program_bake_pipe=program_bake_pipe,
            hard_muted_outputs=_flat_hard_muted_outputs(
                text, summary.get("playback_channels")
            ),
            program_dest_map=flat_graph_program_dest_map(
                topology, contract, width=summary.get("playback_channels") or 0
            ),
            required_mono_fold=required_mono_fold,
            mono_fold_proved=(
                required_mono_fold is not None
                and _flat_mono_fold_proved(text, required_mono_fold)
            ),
        )
    elif camilla_class == "active_startup_candidate":
        graph = _active_graph_allowed(
            text,
            topology,
            contract,
            config_path=path_s,
            summary=summary,
            staged_config=staged_config,
            bass_profile_summary=bass_profile_summary,
        )
    elif camilla_class == CAMILLA_CLASS_ACTIVE_PARKED:
        # No staged-metadata authority here on purpose: a parked graph is
        # derived from the saved topology alone and claims no commissioning
        # provenance, so there is nothing for staged metadata to attest. Its
        # safety rests entirely on the structural all-muted proof.
        graph = _parked_graph_allowed(
            text,
            contract,
            config_path=path_s,
            summary=summary,
        )
    else:
        graph = GraphSafety(
            classification=GRAPH_UNKNOWN,
            allowed=False,
            config_path=path_s,
            camilla_classification=camilla_class,
            playback_device=summary.get("playback_device"),
            playback_channels=summary.get("playback_channels"),
            issues=(
                _issue(
                    "blocker",
                    "camilla_graph_unknown_for_runtime_contract",
                    "CamillaDSP graph is not a known flat or active-speaker graph",
                ),
            ),
            details={"volume_limit_ok": bool(summary.get("volume_limit_ok"))},
        )

    issues = topology_issues + graph_issues
    if issues:
        # A PROVED-PARKED graph is refused only by its OWN blockers (#2145).
        #
        # Every other classification is refused whenever ANYTHING is wrong,
        # because every other graph drives the DAC: if the saved topology is
        # half-assigned, a graph built against it can send the wrong band to a
        # tweeter. The parked graph cannot. Its safety is STRUCTURAL and was
        # just proved by `_parked_graph_allowed` against the graph's own bytes:
        # a `File` sink, a pipeline that is exhaustively one Mixer plus one
        # mute-only Filter per output, and a wired hard mute on every output.
        #
        # The load-bearing pair is the pipeline exactness plus the hard mutes —
        # NOT the File sink on its own. A File sink is not proof that no DAC is
        # reached: the program-bake exemption in this same function exists
        # because a File sink can feed outputd's pipe, and outputd drives the
        # DAC. So the silence guarantee is that every output is hard-muted and
        # the pipeline provably cannot add anything back, whatever consumes the
        # sink. ("Exhaustively" bounds the PIPELINE steps; the mixer's internal
        # mapping is checked by the mute proof, not by step counting.)
        #
        # None of those facts can be falsified by a topology blocker, so
        # refusing on one only prevented the box from parking — it never made it
        # quieter.
        #
        # Keyed on the VERDICT (`GRAPH_PARKED_ALL_MUTED`), not on the claimed
        # input class: `_parked_graph_allowed` returns that classification if
        # and only if all four structural facts hold, and `GRAPH_UNSAFE`
        # otherwise. So a graph that merely CLAIMS the parked source marker and
        # fails its proof cannot reach the exemption.
        #
        # Stated honestly, that key is defence in depth rather than the load-
        # bearing guard: `graph.allowed` is already False for a failed proof, so
        # keying on the claimed marker instead would refuse the same graphs, and
        # no test distinguishes the two (verified by mutation — the swap
        # survives the suite). It is kept because it makes the exemption's
        # precondition legible at the point of use, and because it stays correct
        # if a future classification ever returns `allowed=True` alongside a
        # parked-looking marker.
        #
        # `graph_issues` still gate: they describe this graph's own text, so
        # they are exactly the class of defect that CAN make it unsafe.
        # `topology_issues` are reported either way — the deploy proceeds, but
        # it proceeds LOUDLY, with each blocker in the transcript.
        parked_proof_holds = graph.classification == GRAPH_PARKED_ALL_MUTED
        gating = graph_issues if parked_proof_holds else issues
        return GraphSafety(
            classification=graph.classification,
            allowed=graph.allowed and not gating,
            config_path=graph.config_path,
            camilla_classification=graph.camilla_classification,
            playback_device=graph.playback_device,
            playback_channels=graph.playback_channels,
            issues=tuple(issues) + graph.issues,
            details=graph.details,
        )
    return graph


def _unsafe_boundary(code: str, message: str) -> GraphSafety:
    return GraphSafety(
        classification=GRAPH_UNSAFE,
        allowed=False,
        issues=(_issue("blocker", code, message),),
    )


def _json_mapping(raw: bytes | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _normalized_graph_fingerprint(text: str) -> str | None:
    try:
        parsed = yaml.safe_load(text)
        if not isinstance(parsed, dict) or not parsed:
            return None
        return NormalizedActiveRawIdentity(parsed).active_raw_fingerprint
    except (RecursionError, UnicodeError, ValueError, yaml.YAMLError):
        return None


def _evaluated_profile_summary(
    *,
    topology: OutputTopology,
    applied_baseline_state: Mapping[str, Any] | None,
    profile_bytes: bytes | None,
) -> dict[str, Any]:
    """Translate exact profile bytes into disk-free graph evidence."""

    if profile_bytes is None:
        return {"authority_valid": True, "runtime_block_required": False}
    raw = _json_mapping(profile_bytes)
    if raw is None:
        return {"authority_valid": True, "runtime_block_required": False}
    try:
        from jasper.bass_extension.profile import (
            BassExtensionProfile,
            evaluate_loaded_bass_extension_profile,
        )

        profile = BassExtensionProfile.from_dict(raw)
        evaluation = evaluate_loaded_bass_extension_profile(
            profile,
            topology=topology,
            applied_baseline_state=applied_baseline_state,
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return {"authority_valid": True, "runtime_block_required": False}
    if evaluation.status != "accepted":
        return {"authority_valid": True, "runtime_block_required": False}
    adapter_id = str(profile.enclosure["adapter_id"])
    if adapter_id != "sealed_v1":
        return {"authority_valid": True, "runtime_block_required": False}
    natural = profile.targets[-1]
    protected = all(target.subsonic is not None for target in profile.targets)
    return {
        "authority_valid": protected,
        "runtime_block_required": True,
        "bass_owner_channels": list(profile.bass_owner["channels"]),
        "natural": {
            "fp_hz": natural.fp_hz,
            "qp": natural.qp,
            "boost_headroom_db": natural.boost_headroom_db,
            "subsonic": (
                dict(natural.subsonic) if natural.subsonic is not None else None
            ),
        },
    }


def _intent_profile_bytes(
    intent: Mapping[str, Any],
    role: str,
) -> bytes | None | object:
    profiles = intent.get("profiles")
    entry = profiles.get(role) if isinstance(profiles, Mapping) else None
    if not isinstance(entry, Mapping) or type(entry.get("present")) is not bool:
        return _INVALID_BYTES
    text = entry.get("bytes")
    digest = entry.get("sha256")
    if entry["present"] is False:
        return None if text is None and digest is None else _INVALID_BYTES
    if not isinstance(text, str):
        return _INVALID_BYTES
    try:
        raw = text.encode("utf-8")
    except UnicodeEncodeError:
        return _INVALID_BYTES
    if digest != hashlib.sha256(raw).hexdigest():
        return _INVALID_BYTES
    return raw


_INVALID_BYTES = object()


def _snapshot_profile_summary(
    *,
    topology: OutputTopology,
    graph_text: str,
    applied_baseline_state: Mapping[str, Any] | None,
    profile_bytes: bytes | None,
    intent_bytes: bytes | None,
    selected_config_path: str | None,
) -> dict[str, Any]:
    if intent_bytes is None:
        return _evaluated_profile_summary(
            topology=topology,
            applied_baseline_state=applied_baseline_state,
            profile_bytes=profile_bytes,
        )
    intent = _json_mapping(intent_bytes)
    graph_fingerprint = _normalized_graph_fingerprint(graph_text)
    if (
        intent is None
        or intent.get("kind") != "jts_bass_extension_apply_intent"
        or type(intent.get("schema_version")) is not int
        or intent.get("schema_version") != 1
        or graph_fingerprint is None
    ):
        return {"authority_valid": False, "runtime_block_required": False}
    graphs = intent.get("graphs")
    config = intent.get("config")
    operation_id = intent.get("operation_id")
    if (
        not isinstance(graphs, Mapping)
        or not isinstance(config, Mapping)
        or not isinstance(operation_id, str)
        or len(operation_id) != 32
        or any(ch not in "0123456789abcdef" for ch in operation_id)
    ):
        return {"authority_valid": False, "runtime_block_required": False}
    try:
        from jasper.audio_measurement.evidence_identity import ExactDspStateIdentity

        ExactDspStateIdentity.from_mapping(intent.get("predecessor_identity"))
        config_path = config["path"]
        mode = config["mode"]
        predecessor_graph = config["predecessor_bytes"]
        desired_graph = config["desired_bytes"]
    except (KeyError, TypeError, ValueError):
        return {"authority_valid": False, "runtime_block_required": False}
    if (
        not isinstance(config_path, str)
        or not config_path
        or config_path.strip() != config_path
        or type(mode) is not int
        or mode < 0
        or mode > 0o7777
        or not isinstance(predecessor_graph, str)
        or not isinstance(desired_graph, str)
        or intent.get("boot_selector_target") != config_path
        or selected_config_path != config_path
    ):
        return {"authority_valid": False, "runtime_block_required": False}
    try:
        predecessor_sha256 = hashlib.sha256(
            predecessor_graph.encode("utf-8")
        ).hexdigest()
        desired_sha256 = hashlib.sha256(
            desired_graph.encode("utf-8")
        ).hexdigest()
    except UnicodeEncodeError:
        return {"authority_valid": False, "runtime_block_required": False}
    if (
        config.get("predecessor_sha256") != predecessor_sha256
        or config.get("desired_sha256") != desired_sha256
        or graphs.get("predecessor")
        != _normalized_graph_fingerprint(predecessor_graph)
        or graphs.get("desired") != _normalized_graph_fingerprint(desired_graph)
    ):
        return {"authority_valid": False, "runtime_block_required": False}
    predecessor = _intent_profile_bytes(intent, "predecessor")
    desired = _intent_profile_bytes(intent, "desired")
    if (
        predecessor is _INVALID_BYTES
        or desired is _INVALID_BYTES
        or desired is None
        or profile_bytes not in (predecessor, desired)
    ):
        return {"authority_valid": False, "runtime_block_required": False}
    matching_profiles = []
    if graphs.get("predecessor") == graph_fingerprint:
        matching_profiles.append(predecessor)
    if graphs.get("desired") == graph_fingerprint:
        matching_profiles.append(desired)
    # A no-block replacement can legitimately have identical predecessor and
    # desired graph fingerprints.  The exact persisted profile bytes select
    # the corresponding evaluation without widening authority to a third pair.
    if profile_bytes not in matching_profiles:
        return {"authority_valid": False, "runtime_block_required": False}
    return _evaluated_profile_summary(
        topology=topology,
        applied_baseline_state=applied_baseline_state,
        profile_bytes=profile_bytes,
    )


def _classify_bass_extension_snapshot(
    topology: OutputTopology,
    *,
    graph_text: str,
    config_path: str | None,
    applied_baseline_bytes: bytes | None,
    applied_baseline_state: Mapping[str, Any] | None,
    profile_bytes: bytes | None,
    intent_bytes: bytes | None,
    staged_metadata_bytes: bytes | None,
) -> GraphSafety:
    applied = (
        dict(applied_baseline_state)
        if isinstance(applied_baseline_state, Mapping)
        else _json_mapping(applied_baseline_bytes)
    )
    # Canonical persisted snapshots always carry an explicit staged-authority
    # mapping. Missing, malformed, or non-object bytes become stable empty
    # evidence and cannot authorize staged-dependent graphs. Direct low-level
    # in-memory composition calls retain ``staged_config=None`` and their
    # independent graph-only proof.
    staged = _json_mapping(staged_metadata_bytes) or {}
    bass_summary = _snapshot_profile_summary(
        topology=topology,
        graph_text=graph_text,
        applied_baseline_state=applied,
        profile_bytes=profile_bytes,
        intent_bytes=intent_bytes,
        selected_config_path=config_path,
    )
    graph = classify_camilla_graph(
        config_path,
        topology,
        text=graph_text,
        staged_config=staged,
        bass_profile_summary=bass_summary,
    )
    return replace(
        graph,
        details={
            **graph.details,
            "bass_extension_profile_summary": dict(bass_summary),
        },
    )


def _read_optional_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _candidate_locator(
    kind: str,
    *,
    explicit_path: Path | None,
    applied_bytes: bytes | None,
    staged_bytes: bytes | None,
) -> Path | None:
    if kind == "explicit":
        return explicit_path
    authority = _json_mapping(
        applied_bytes if kind == "applied_baseline" else staged_bytes
    )
    config = authority.get("config") if isinstance(authority, Mapping) else None
    raw = config.get("path") if isinstance(config, Mapping) else None
    return Path(raw) if isinstance(raw, str) and raw.strip() == raw else None


def classify_bass_extension_graph(
    topology: OutputTopology,
    *,
    evidence_source: Literal["persisted_boot", "persisted_candidate", "desired"],
    statefile_path: Path | None = None,
    candidate_kind: Literal["explicit", "applied_baseline", "staged_all_muted"] | None = None,
    candidate_path: Path | None = None,
    graph_text: str | None = None,
    applied_baseline_path: Path | None = None,
    applied_baseline_state: Mapping[str, Any] | None = None,
    profile_path: Path | None = None,
    intent_path: Path | None = None,
    staged_metadata_path: Path | None = None,
    desired_profile: "BassExtensionProfile | None | object" = (
        _BASS_PROFILE_EVIDENCE_OMITTED
    ),
) -> GraphSafety:
    """Canonical synchronous graph/evidence boundary."""

    if evidence_source == "desired":
        from jasper.bass_extension.profile import BassExtensionProfile

        if (
            any(path is not None for path in (
                statefile_path, candidate_path, applied_baseline_path,
                profile_path, intent_path, staged_metadata_path,
            ))
            or candidate_kind is not None
            or not isinstance(graph_text, str)
            or not isinstance(applied_baseline_state, Mapping)
            or not (
                desired_profile is None
                or isinstance(desired_profile, BassExtensionProfile)
            )
        ):
            return _unsafe_boundary("bass_extension_source_invalid", "desired evidence is incomplete")
        desired_bytes = None
        if desired_profile is not None:
            desired_bytes = (
                json.dumps(desired_profile.to_dict(), indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
        return _classify_bass_extension_snapshot(
            topology,
            graph_text=graph_text,
            config_path=None,
            applied_baseline_bytes=None,
            applied_baseline_state=applied_baseline_state,
            profile_bytes=desired_bytes,
            intent_bytes=None,
            staged_metadata_bytes=None,
        )

    if (
        graph_text is not None
        or applied_baseline_state is not None
        or desired_profile is not _BASS_PROFILE_EVIDENCE_OMITTED
        or applied_baseline_path is None
        or profile_path is None
        or intent_path is None
        or staged_metadata_path is None
    ):
        return _unsafe_boundary("bass_extension_source_invalid", "persisted evidence paths are incomplete")
    if evidence_source == "persisted_boot":
        if statefile_path is None or candidate_kind is not None or candidate_path is not None:
            return _unsafe_boundary("bass_extension_source_invalid", "persisted boot evidence is invalid")
    elif evidence_source == "persisted_candidate":
        if statefile_path is not None or candidate_kind is None:
            return _unsafe_boundary("bass_extension_source_invalid", "persisted candidate evidence is invalid")
        if (candidate_kind == "explicit") != (candidate_path is not None):
            return _unsafe_boundary("bass_extension_candidate_invalid", "candidate path provenance is invalid")
    else:
        return _unsafe_boundary("bass_extension_source_invalid", "unknown evidence source")

    for _attempt in range(2):
        try:
            applied1 = _read_optional_bytes(applied_baseline_path)
            intent1 = _read_optional_bytes(intent_path)
            profile1 = _read_optional_bytes(profile_path)
            staged1 = _read_optional_bytes(staged_metadata_path)
            if evidence_source == "persisted_boot":
                assert statefile_path is not None
                selector1 = statefile_path.read_bytes()
                selected1_s = parse_camilla_statefile_config_path(selector1.decode("utf-8"))
                if not selected1_s:
                    continue
                selected_path = Path(selected1_s)
            else:
                assert candidate_kind is not None
                selected_path = _candidate_locator(
                    candidate_kind,
                    explicit_path=candidate_path,
                    applied_bytes=applied1,
                    staged_bytes=staged1,
                )
                if selected_path is None:
                    continue
            selector1 = None
            selected1 = selected_path.read_bytes()
            selected2 = selected_path.read_bytes()
            if evidence_source == "persisted_boot":
                selector2 = statefile_path.read_bytes()
                selected2_s = parse_camilla_statefile_config_path(selector2.decode("utf-8"))
                if selected2_s != str(selected_path):
                    continue
            else:
                selector2 = None
            staged2 = _read_optional_bytes(staged_metadata_path)
            profile2 = _read_optional_bytes(profile_path)
            intent2 = _read_optional_bytes(intent_path)
            applied2 = _read_optional_bytes(applied_baseline_path)
        except (OSError, UnicodeError, ValueError):
            continue
        if not all((
            applied1 == applied2,
            intent1 == intent2,
            profile1 == profile2,
            staged1 == staged2,
            selected1 == selected2,
        )):
            continue
        if evidence_source == "persisted_candidate":
            locator2 = _candidate_locator(
                str(candidate_kind),
                explicit_path=candidate_path,
                applied_bytes=applied2,
                staged_bytes=staged2,
            )
            if locator2 != selected_path:
                continue
        try:
            selected_text = selected1.decode("utf-8")
        except UnicodeError:
            continue
        return _classify_bass_extension_snapshot(
            topology,
            graph_text=selected_text,
            config_path=str(selected_path),
            applied_baseline_bytes=applied1,
            applied_baseline_state=None,
            profile_bytes=profile1,
            intent_bytes=intent1,
            staged_metadata_bytes=staged1,
        )
    return _unsafe_boundary("bass_extension_snapshot_unstable", "graph authority changed while it was read")


async def _invoke_active_graph_reader(
    reader: Callable[[], Awaitable[str | None]],
) -> str | None:
    """Invoke one live CamillaDSP query inside a task so failures become evidence.

    Used for both queries the live boundary makes — reading the running graph
    and canonicalizing the selected file — since either can fail the same ways.
    """

    return await reader()


async def classify_active_bass_extension_graph(
    topology: OutputTopology,
    *,
    statefile_path: Path,
    read_active_graph_text: Callable[[], Awaitable[str | None]],
    canonicalize_graph_text: Callable[[str], Awaitable[str | None]],
    applied_baseline_path: Path,
    profile_path: Path,
    intent_path: Path,
    staged_metadata_path: Path,
) -> GraphSafety:
    """Canonical live-active boundary with readback inside the sandwich.

    Both sides of the fingerprint comparison MUST come from CamillaDSP.
    ``read_active_graph_text`` already does — it is CamillaDSP's own
    re-serialization of the running graph (``GetConfig``), which default-fills
    every key the config omits. The statefile-selected file does NOT: it is
    JTS-authored text whose emitters leave defaulted keys out entirely. So it
    is put through CamillaDSP's ``ReadConfig`` — ``canonicalize_graph_text``,
    i.e. :meth:`jasper.camilla.CamillaController.normalize_config_raw` — before
    being fingerprinted. Comparing the raw file against the readback instead
    can never match, and fails CLOSED, so the whole gate silently refuses
    everything (the 2026-08-06 bonded-pair outage).

    ``canonicalize_graph_text`` is a required caller-injected seam on purpose.
    Injected, so CamillaDSP stays the single authority on its own schema and
    this module holds no copy of its defaults. Required rather than defaulted,
    so a new caller cannot re-open the same silent trap by omitting it.
    """

    reason = "live graph authority could not be proved"
    for _attempt in range(2):
        try:
            applied1 = _read_optional_bytes(applied_baseline_path)
            intent1 = _read_optional_bytes(intent_path)
            profile1 = _read_optional_bytes(profile_path)
            staged1 = _read_optional_bytes(staged_metadata_path)
            selector1 = statefile_path.read_bytes()
            selected1_s = parse_camilla_statefile_config_path(selector1.decode("utf-8"))
            if not selected1_s:
                reason = "the CamillaDSP statefile names no config"
                continue
            selected_path = Path(selected1_s)
            selected1 = selected_path.read_bytes()
            selected_text = selected1.decode("utf-8")
        except (OSError, UnicodeError, ValueError):
            reason = "the graph authority could not be read"
            continue

        # Both CamillaDSP queries share the ONE await window this sandwich
        # brackets, so the authority re-read below still covers every await.
        # Safe to gather because every caller hands both callables the SAME
        # CamillaController, whose `_call` holds its lock for the whole call —
        # so these serialize rather than interleaving two requests on one
        # pycamilladsp websocket.
        active_result, canonical_result = await asyncio.gather(
            _invoke_active_graph_reader(read_active_graph_text),
            _invoke_active_graph_reader(
                lambda: canonicalize_graph_text(selected_text),
            ),
            return_exceptions=True,
        )
        for result in (active_result, canonical_result):
            if isinstance(result, asyncio.CancelledError):
                raise result
        if isinstance(active_result, BaseException):
            reason = "the running CamillaDSP graph could not be read"
            continue
        if isinstance(canonical_result, BaseException):
            reason = (
                "CamillaDSP could not canonicalize the statefile-selected config"
            )
            continue
        active_text = active_result
        canonical_text = canonical_result

        try:
            selected2 = selected_path.read_bytes()
            selector2 = statefile_path.read_bytes()
            selected2_s = parse_camilla_statefile_config_path(selector2.decode("utf-8"))
            staged2 = _read_optional_bytes(staged_metadata_path)
            profile2 = _read_optional_bytes(profile_path)
            intent2 = _read_optional_bytes(intent_path)
            applied2 = _read_optional_bytes(applied_baseline_path)
        except (OSError, UnicodeError, ValueError):
            reason = "the graph authority could not be re-read"
            continue
        if (
            selected2_s != str(selected_path)
            or not all((
                applied1 == applied2,
                intent1 == intent2,
                profile1 == profile2,
                staged1 == staged2,
                selected1 == selected2,
            ))
        ):
            reason = "the graph authority changed while it was read"
            continue
        if not isinstance(active_text, str) or not isinstance(canonical_text, str):
            reason = "CamillaDSP returned no graph to compare"
            continue
        active_fingerprint = _normalized_graph_fingerprint(active_text)
        if (
            active_fingerprint is None
            or active_fingerprint != _normalized_graph_fingerprint(canonical_text)
        ):
            reason = (
                "the running CamillaDSP graph does not match the "
                "statefile-selected config"
            )
            continue
        return _classify_bass_extension_snapshot(
            topology,
            graph_text=selected_text,
            config_path=str(selected_path),
            applied_baseline_bytes=applied1,
            applied_baseline_state=None,
            profile_bytes=profile1,
            intent_bytes=intent1,
            staged_metadata_bytes=staged1,
        )
    return _unsafe_boundary("bass_extension_active_snapshot_unstable", reason)


def _config_path_from_statefile_with_reason(
    statefile_path: str | Path,
    *,
    missing: str,
    unreadable: str,
    config_missing: str,
    target_missing: str,
) -> tuple[Path | None, str | None]:
    target = Path(statefile_path)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, missing
    except OSError as exc:
        return None, f"{unreadable}:{type(exc).__name__}"

    config_path_s = parse_camilla_statefile_config_path(text)
    if not config_path_s:
        return None, config_missing

    config_path = Path(config_path_s)
    if not config_path.exists():
        return None, target_missing
    return config_path, None


def _outputd_endpoint_width(
    graph: GraphSafety,
    cap_channels: int,
    *,
    classifications: frozenset[str] = OUTPUTD_ENDPOINT_GRAPH_CLASSIFICATIONS,
    devices: frozenset[str] = OUTPUTD_LEGAL_ENDPOINT_DEVICES,
) -> tuple[int | None, str | None, str | None]:
    """The width outputd should open, plus WHICH endpoint device was accepted.

    Returns ``(width, problem, device)``. The device is what makes the
    reconciler's ring-endpoint marker derive from THIS classification rather
    than a second, independent read of the graph — one classification, one
    answer, so the marker and the width can never disagree about the graph they
    describe.
    """
    if not graph.allowed:
        issue = graph.issues[0]["code"] if graph.issues else graph.classification
        return None, f"active_graph_unsafe:{issue}", None
    if graph.classification not in classifications:
        return None, f"active_graph_not_outputd_endpoint:{graph.classification}", None
    device = graph.playback_device
    if device not in devices:
        return None, "active_outputd_lane_missing", None

    got = int(graph.playback_channels or 0)
    if got < 2 or got > cap_channels:
        return (
            None,
            f"active_graph_width_out_of_range got={got} cap={cap_channels}",
            None,
        )
    return got, None, device


def outputd_active_lane_decision(
    cap_channels: int,
    *,
    statefile_path: str | Path | None = None,
    crossover_statefile_path: str | Path | None = None,
    topology: OutputTopology | None = None,
    topology_path: str | Path | None = None,
    applied_baseline_path: str | Path | None = None,
    profile_path: str | Path | None = None,
    intent_path: str | Path | None = None,
    staged_metadata_path: str | Path | None = None,
) -> OutputdActiveLaneDecision:
    """Decide whether outputd may open its active content lane.

    This does not select or load a CamillaDSP graph. It only proves that the
    graph already live in the relevant CamillaDSP statefile(s) is an outputd
    endpoint graph, then returns the width outputd should open.
    """

    try:
        cap = int(cap_channels)
    except (TypeError, ValueError):
        return OutputdActiveLaneDecision(
            ok=False, width=None, reason="active_graph_cap_channels_invalid",
        )
    if cap < 2:
        return OutputdActiveLaneDecision(
            ok=False, width=None, reason=f"active_graph_cap_channels_invalid:{cap}",
        )

    from jasper.active_speaker.baseline_profile import baseline_profile_state_path
    from jasper.active_speaker.staging import staged_metadata_path as default_staged_path
    from jasper.bass_extension import BASS_EXTENSION_APPLY_INTENT_PATH
    from jasper.bass_extension.profile import DEFAULT_PROFILE_PATH

    primary_statefile = Path(statefile_path or DEFAULT_CAMILLA_STATEFILE)
    _selected, primary_problem = _config_path_from_statefile_with_reason(
        primary_statefile,
        missing="camilla_statefile_missing",
        unreadable="camilla_statefile_unreadable",
        config_missing="camilla_statefile_config_path_missing",
        target_missing="active_config_missing",
    )
    if primary_problem:
        return OutputdActiveLaneDecision(
            ok=False,
            width=None,
            reason=primary_problem,
        )
    topology = topology or load_output_topology_strict(topology_path)
    authority = {
        "applied_baseline_path": Path(
            applied_baseline_path or baseline_profile_state_path()
        ),
        "profile_path": Path(profile_path or DEFAULT_PROFILE_PATH),
        "intent_path": Path(intent_path or BASS_EXTENSION_APPLY_INTENT_PATH),
        "staged_metadata_path": Path(
            staged_metadata_path or default_staged_path()
        ),
    }
    primary_graph = classify_bass_extension_graph(
        topology,
        evidence_source="persisted_boot",
        statefile_path=primary_statefile,
        **authority,
    )
    width, problem, endpoint_device = _outputd_endpoint_width(primary_graph, cap)
    if width is not None:
        return OutputdActiveLaneDecision(
            ok=True,
            width=width,
            reason="active_outputd_endpoint",
            source="primary_statefile",
            primary_graph=primary_graph,
            endpoint_graph=primary_graph,
            endpoint_device=endpoint_device,
        )

    if primary_graph.classification != GRAPH_PROGRAM_BAKE_PIPE:
        if not primary_graph.allowed:
            _unused, authority_problem = _config_path_from_statefile_with_reason(
                primary_statefile,
                missing="camilla_statefile_missing",
                unreadable="camilla_statefile_unreadable",
                config_missing="camilla_statefile_config_path_missing",
                target_missing="active_config_missing",
            )
            if authority_problem:
                problem = authority_problem
        return OutputdActiveLaneDecision(
            ok=False,
            width=None,
            reason=problem or f"active_graph_not_outputd_endpoint:{primary_graph.classification}",
            primary_graph=primary_graph,
        )

    crossover_statefile = Path(
        crossover_statefile_path or DEFAULT_CAMILLA2_STATEFILE
    )
    crossover_graph = classify_bass_extension_graph(
        topology,
        evidence_source="persisted_boot",
        statefile_path=crossover_statefile,
        **authority,
    )
    if not crossover_graph.allowed:
        _unused, crossover_problem = _config_path_from_statefile_with_reason(
            crossover_statefile,
            missing="camilla2_statefile_missing",
            unreadable="camilla2_statefile_unreadable",
            config_missing="camilla2_statefile_config_path_missing",
            target_missing="active_crossover_config_missing",
        )
        crossover_problem = crossover_problem or (
            crossover_graph.issues[0]["code"]
            if crossover_graph.issues
            else crossover_graph.classification
        )
        return OutputdActiveLaneDecision(
            ok=False,
            width=None,
            reason=f"program_bake_pipe_without_active_crossover:{crossover_problem}",
            primary_graph=primary_graph,
        )

    width, problem, endpoint_device = _outputd_endpoint_width(
        crossover_graph,
        cap,
        classifications=frozenset((GRAPH_DRIVER_DOMAIN_BASELINE,)),
    )
    if width is None:
        return OutputdActiveLaneDecision(
            ok=False,
            width=None,
            reason=f"program_bake_pipe_without_active_crossover:{problem}",
            primary_graph=primary_graph,
            endpoint_graph=crossover_graph,
        )

    return OutputdActiveLaneDecision(
        ok=True,
        width=width,
        reason="active_leader_crossover_endpoint",
        source="crossover_statefile",
        primary_graph=primary_graph,
        endpoint_graph=crossover_graph,
        endpoint_device=endpoint_device,
    )


def parked_muted_config_path(path: str | Path | None = None) -> Path:
    """The deterministic on-disk location of the PARKED graph.

    Lives beside the staged startup config in the generated-config dir (staging
    owns that directory constant, so there is one spelling of it).
    """

    from jasper.active_speaker.camilla_yaml import PARKED_CONFIG_NAME
    from jasper.active_speaker.staging import DEFAULT_CAMILLA_CONFIG_DIR

    return Path(path) if path else Path(DEFAULT_CAMILLA_CONFIG_DIR) / PARKED_CONFIG_NAME


def active_graph_is_parked(config_path: str | Path | None) -> bool:
    """True when ``config_path`` holds the parked graph.

    Content-keyed on the emitted ``# Source:`` provenance marker, not on the
    filename — a renamed or hand-copied file must not be able to claim (or
    disclaim) parked status. Fail-soft: False on any read or parse problem, so a
    reporting surface degrades to "not parked" rather than raising. Callers that
    need SAFETY, not reporting, use ``classify_camilla_graph`` — this predicate
    proves nothing about the graph's contents.
    """

    if not config_path:
        return False
    try:
        text = Path(config_path).read_text(encoding="utf-8")
        summary = classify_camilla_config_text(text)
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError, yaml.YAMLError):
        return False
    return summary.get("classification") == CAMILLA_CLASS_ACTIVE_PARKED


def build_parked_muted_graph(
    topology: OutputTopology,
    *,
    config_path: str | Path | None = None,
) -> tuple[str | None, GraphSafety]:
    """Build + independently verify the PARKED graph for ``topology``.

    Pure: derives the graph from the saved topology alone (no disk write, no
    hardware probe, no output-route resolution — the sink is a File, so there is
    no DAC lane to resolve) and returns it only alongside the verifier's verdict,
    so no caller can persist parked bytes that were not proved safe.
    """

    from jasper.active_speaker.camilla_yaml import emit_active_speaker_parked_config
    from jasper.active_speaker.profile import ActiveSpeakerConfigError

    contract = classify_output_contract(topology)
    # A stereo capture feeds the mixer, so park at least 2 channels: a 1-output
    # topology (a lone subwoofer) would otherwise emit a 2->1 graph whose extra
    # capture channel is silently dropped rather than explicitly muted. The extra
    # muted output costs nothing.
    width = max(_required_output_width(contract), 2)
    try:
        text = emit_active_speaker_parked_config(
            output_count=width,
            topology_id=topology.topology_id,
        )
    except (ActiveSpeakerConfigError, ValueError) as exc:
        return None, _unsafe_boundary(
            "parked_graph_emit_failed",
            f"could not build a parked active-speaker graph: {type(exc).__name__}",
        )
    return text, classify_camilla_graph(
        topology=topology,
        text=text,
        config_path=str(parked_muted_config_path(config_path)),
    )


def parked_safe_graph_decision(
    topology: OutputTopology,
    *,
    config_path: str | Path | None = None,
    reason: str = "temporarily parked before changing saved speaker layout",
) -> SafeGraphDecision:
    """Return the independently-proved all-muted holding graph for topology.

    This is the only temporary graph topology replacement may load before it
    writes new speaker intent.  It has a File sink and every output terminally
    muted, so it is legal for both the old and proposed topology.
    """

    contract = classify_output_contract(topology)
    text, graph = build_parked_muted_graph(topology, config_path=config_path)
    if text is not None and graph.allowed:
        return SafeGraphDecision(
            status=PARKED_MUTED_STATUS,
            selected_config_path=str(parked_muted_config_path(config_path)),
            reason=reason,
            topology_contract=contract,
            fallback_graph=graph,
        )
    return SafeGraphDecision(
        status="blocked",
        selected_config_path=None,
        reason="could not prove the parked all-muted graph",
        topology_contract=contract,
        fallback_graph=graph,
        issues=graph.issues,
    )


def _linearization_headroom_regression(
    *graphs: GraphSafety | None,
) -> tuple[dict[str, str], ...]:
    """The numeric headroom refusals carried by any of ``graphs``.

    Empty whenever none of them failed THAT way — which includes every graph
    that is allowed, every graph refused on shape, and the absent-graph case.
    So a caller can ask "did this box's own active graph regress on the
    headroom arithmetic?" without re-deriving the condition, and a new refusal
    reason cannot silently start firing a migration guard written for this one.

    DE-DUPLICATED, order-preserving, because on a commissioned box the two
    graphs asked here are usually the SAME FILE: the applied-baseline authority
    points at the artifact the statefile already loads, so both classifications
    carry the identical refusal and the deploy transcript printed every blocker
    twice. Keyed on the whole issue rather than on the code, so two branches
    that genuinely both regressed still report one line each — the message
    names the role and the numbers, which is exactly what a reader needs when
    the woofer and the tweeter fail differently.
    """
    seen: list[dict[str, str]] = []
    for graph in graphs:
        if graph is None:
            continue
        for issue in graph.issues:
            if issue.get("code") != LINEARIZATION_HEADROOM_UNPROVEN_CODE:
                continue
            if issue not in seen:
                seen.append(issue)
    return tuple(seen)


def safe_graph_for_current_topology(
    topology: OutputTopology | None = None,
    *,
    statefile_path: str | Path | None = None,
    current_config_path: str | Path | None = None,
    preferred_config_path: str | Path | None = None,
    flat_config_path: str | Path = DEFAULT_FLAT_OUTPUTD_CONFIG,
    parked_config_path: str | Path | None = None,
    coupling: str | None = None,
    applied_baseline_path: str | Path | None = None,
    profile_path: str | Path | None = None,
    intent_path: str | Path | None = None,
    staged_metadata_path: str | Path | None = None,
    staged_startup_hold_path: str | Path | None = None,
    consider_applied_baseline: bool = True,
    staged_config: Mapping[str, Any] | None = None,
) -> SafeGraphDecision:
    """Select the only safe persisted CamillaDSP graph for this topology.

    **A ROLEFUL box's ring is a different question, and this function answers it
    differently.** The flat branch selects one PRE-RENDERED file, because a flat
    graph is the same on every box. A roleful box's graph is per-speaker —
    its crossover, its corrections — so there is nothing to pick between: the
    graphs that carry it (the applied baseline, the staged all-muted startup) are
    emitted by the commissioning path, naming the ACTIVE ring, because
    ``resolve_output_layout`` is the single place that chooses the active
    endpoint device. So the roleful branches below are
    unchanged and correct on an armed box — they preserve or select whatever the
    box's own graphs declare.

    The residual an armed roleful box carries is therefore a STALE ARTIFACT, not
    a wrong selection: if its on-disk baseline still names the pre-arm ALSA lane,
    preserving it is the correct thing to do with the graph the box has, and the
    box de-arms itself on the next CamillaDSP restart. Closing that is what
    ``jasper-active-speaker baseline-reemit --endpoint ring`` is FOR — it
    publishes the re-emitted graph over the artifact this function selects and
    repoints the statefile at it, which is also why it is step ONE of the arm
    ladder rather than a tidy-up after it. The doctor's ``check_fanin_coupling``
    reports the gap in the meantime — it derives the expected playback device
    from the endpoint marker, so it names the exact mismatch rather than reading
    green through it. This function deliberately does NOT add a refusal of its
    own: blocking here would
    turn a recoverable stale artifact into a box that cannot seed a graph at all.

    The PARKED shape needs nothing either way — its sink is a ``File``, so it is
    DAC- and transport-agnostic by construction."""

    from jasper.active_speaker.baseline_profile import baseline_profile_state_path
    from jasper.active_speaker.staging import staged_metadata_path as default_staged_path
    from jasper.bass_extension import BASS_EXTENSION_APPLY_INTENT_PATH
    from jasper.bass_extension.profile import DEFAULT_PROFILE_PATH

    if staged_config not in (None, {}):
        raise TypeError(
            "in-memory staged_config authority is no longer accepted; "
            "pass staged_metadata_path"
        )
    topology = topology or load_output_topology_strict()
    contract = classify_output_contract(topology)
    # Empty is a deliberate runtime state, not implicit stereo.  Decide it
    # before looking at the current graph so a previously-loaded flat graph
    # cannot be preserved after reset or on a fresh install.
    if contract.classification == CONTRACT_UNCONFIGURED:
        return parked_safe_graph_decision(
            topology,
            config_path=parked_config_path,
            reason="no speaker layout is configured; parked with every output muted",
        )
    statefile = Path(statefile_path or DEFAULT_CAMILLA_STATEFILE)
    applied_path = Path(applied_baseline_path or baseline_profile_state_path())
    bass_path = Path(profile_path or DEFAULT_PROFILE_PATH)
    apply_intent_path = Path(intent_path or BASS_EXTENSION_APPLY_INTENT_PATH)
    staged_path_authority = Path(staged_metadata_path or default_staged_path())

    authority = {
        "applied_baseline_path": applied_path,
        "profile_path": bass_path,
        "intent_path": apply_intent_path,
        "staged_metadata_path": staged_path_authority,
    }
    if current_config_path:
        current_path = str(current_config_path)
        current_graph = classify_bass_extension_graph(
            topology,
            evidence_source="persisted_candidate",
            candidate_kind="explicit",
            candidate_path=Path(current_config_path),
            **authority,
        )
    else:
        current_graph = classify_bass_extension_graph(
            topology,
            evidence_source="persisted_boot",
            statefile_path=statefile,
            **authority,
        )
        current_path = current_graph.config_path
    preferred_graph = (
        classify_bass_extension_graph(
            topology,
            evidence_source="persisted_candidate",
            candidate_kind="applied_baseline",
            **authority,
        )
        if consider_applied_baseline
        else None
    )
    preferred_path = preferred_graph.config_path if preferred_graph else None
    if preferred_config_path and preferred_path and not _path_matches(
        preferred_config_path, preferred_path
    ):
        preferred_graph = _unsafe_boundary(
            "applied_baseline_locator_mismatch",
            "preferred graph does not match the applied-baseline authority",
        )
    if (
        current_graph
        and current_graph.allowed
        and topology_allows_flat_dac_graph(contract)
        # A program-bake pipe is allowed by the verifier (no DAC, no driver to
        # over-drive) but is NOT a selectable solo graph: its File sink feeds the
        # snapserver FIFO, not the DAC, so preserving it on a solo speaker would
        # leave the DAC silent. Selecting/wiring camilla#1 is a later Stage-B
        # slice; this selector must never pick the pipe bake as a speaker's own
        # output graph.
        and current_graph.classification != GRAPH_PROGRAM_BAKE_PIPE
        # The PARKED graph (#2135) is the same shape of trap and is excluded for
        # the same reason: it is legal for ANY topology (File sink, every output
        # muted), so without this it would be "preserved" forever after reset.
        # Reset first writes the unconfigured, parked state; once the household
        # saves an explicit passive layout, this exclusion lets the selector
        # fall through to `select_flat` below rather than keep /dev/null.
        and current_graph.classification != GRAPH_PARKED_ALL_MUTED
    ):
        return SafeGraphDecision(
            status="preserve_current",
            selected_config_path=current_path,
            reason="current CamillaDSP graph is legal for saved topology",
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
        )
    # An approved active runtime graph drives roleful lanes at program level. It
    # is only legal while the household still vouches for WHICH driver hangs on
    # each of those lanes — a fact this selector was blind to until #2814, so a
    # re-pinned (or explicitly un-confirmed) box re-selected its baseline on the
    # next reconcile and resumed audio through drivers nobody had re-checked.
    # Unconfirmed, both rungs below fall through to the staged all-muted /
    # parked selection; confirming every assigned lane again releases them.
    identity_confirmed = roleful_identity_confirmed(topology, contract)
    if (
        current_graph
        and current_graph.allowed
        and current_graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME
        and identity_confirmed
    ):
        return SafeGraphDecision(
            status="preserve_current",
            selected_config_path=current_path,
            reason="current approved active-speaker runtime graph is legal for saved topology",
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
        )
    # Deadlock guard (re-commission on an already-commissioned box). While a
    # protected startup-load session is deliberately holding the staged
    # all-muted startup anchor as the durable graph, the reconciler that load
    # KICKED must NOT restore the saved baseline over it: doing so moves the
    # durable statefile off the anchor, and commission-load's PRE-AUDIO
    # precondition gate (`commission_active_graph_not_staged`) then refuses,
    # because per-driver commissioning "requires the all-muted staged config to
    # be the persisted boot config first". So the anchor-preserve is
    # hoisted ABOVE the baseline-restore rung below — but ONLY while the hold is
    # in flight. The marker is ephemeral (/run), so a NORMAL boot never sees it
    # and the baseline-restore rung fires exactly as before; a commissioned box
    # still comes back to audio on reboot. Preserving an all-muted anchor is the
    # safe direction (silent, never loud), so this needs no identity gate of its
    # own — #2814's gate stays on the approved-runtime rungs it protects.
    if (
        current_graph
        and current_graph.allowed
        and current_graph.classification == GRAPH_ALL_MUTED_ACTIVE_STARTUP
        and staged_startup_hold_active(staged_startup_hold_path)
    ):
        return SafeGraphDecision(
            status="preserve_current",
            selected_config_path=current_path,
            reason=(
                "protected startup-load hold is active; preserving the staged "
                "all-muted startup anchor for the in-flight commission"
            ),
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
        )
    if (
        preferred_graph
        and preferred_graph.allowed
        and preferred_graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME
        and identity_confirmed
    ):
        return SafeGraphDecision(
            status="select_active_baseline",
            selected_config_path=preferred_path,
            reason="saved applied active-speaker baseline is legal for saved topology",
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
        )
    if (
        current_graph
        and current_graph.allowed
        and current_graph.classification == GRAPH_ALL_MUTED_ACTIVE_STARTUP
    ):
        return SafeGraphDecision(
            status="preserve_current",
            selected_config_path=current_path,
            reason="current all-muted active startup graph is legal for saved topology",
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
        )

    if topology_allows_flat_dac_graph(contract):
        # ONE flat graph. It used to choose between a loopback flat config and a
        # ring sibling by the persisted coupling; under one audio transport
        # (ADR-0100) the flat graph IS the ring graph, so there is nothing to
        # choose and no way to re-seed a box off its transport.
        fallback = classify_bass_extension_graph(
            topology,
            evidence_source="persisted_candidate",
            candidate_kind="explicit",
            candidate_path=Path(flat_config_path),
            **authority,
        )
        if fallback.allowed:
            return SafeGraphDecision(
                status="select_flat",
                selected_config_path=str(flat_config_path),
                reason="saved topology is an explicit valid passive layout",
                topology_contract=contract,
                current_graph=current_graph,
                preferred_graph=preferred_graph,
                fallback_graph=fallback,
            )
        return SafeGraphDecision(
            status="blocked",
            selected_config_path=None,
            reason="flat outputd fallback is unavailable or invalid",
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
            fallback_graph=fallback,
            issues=fallback.issues,
        )

    staged_graph = classify_bass_extension_graph(
        topology,
        evidence_source="persisted_candidate",
        candidate_kind="staged_all_muted",
        **authority,
    )
    staged_path = staged_graph.config_path
    # A commissioned box whose OWN boot graph stopped proving on the headroom
    # arithmetic must not be quietly re-pointed at the all-muted startup graph
    # (#2758). That fall is legal — the staged graph really is safe — and it is
    # exactly what makes it dangerous here: the deploy stays GREEN, the speaker
    # goes SILENT, and it is sticky, because the next deploy preserves the
    # all-muted graph it just selected and nothing on the deploy path re-emits
    # the baseline. Refuse instead, carrying the numbers, so a human is
    # summoned to `baseline-reemit` rather than a household discovering it.
    # Having the DEPLOY run that re-emit itself is issue #2847; this is the
    # half that makes the failure loud, which is the half that has to exist.
    #
    # Narrow on purpose, and each clause earns its place. Only the NUMERIC
    # refusal (`LINEARIZATION_HEADROOM_UNPROVEN_CODE`) fires it: a shape refusal
    # is a different defect with a different remedy, and every OTHER reason a
    # box lands on the staged anchor — mid-commission with no baseline at all,
    # or the #2814 identity-unconfirmed hold, where the graph stays `allowed`
    # and is skipped rather than refused — is a state this ladder is SUPPOSED
    # to resolve silently and green.
    regressed = _linearization_headroom_regression(current_graph, preferred_graph)
    if regressed:
        return SafeGraphDecision(
            status="blocked",
            selected_config_path=None,
            reason=(
                "the active-speaker graph this box boots no longer proves its "
                "own headroom charge; selecting the all-muted startup graph "
                "would silence the speaker on a green deploy. Re-emit the "
                "baseline (`jasper-active-speaker baseline-reemit`) and re-run"
            ),
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
            fallback_graph=staged_graph,
            issues=tuple(regressed),
        )
    if (
        staged_graph
        and staged_graph.allowed
        and staged_graph.classification == GRAPH_ALL_MUTED_ACTIVE_STARTUP
    ):
        return SafeGraphDecision(
            status="select_active_startup",
            selected_config_path=staged_path,
            reason="roleful/protected topology requires the all-muted active startup graph",
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
            fallback_graph=staged_graph,
        )

    issues: list[dict[str, str]] = []
    if current_graph and current_graph.issues:
        issues.extend(current_graph.issues)
    elif current_graph and current_graph.allowed:
        issues.append(_issue(
            "blocker",
            "current_graph_not_persistable",
            (
                f"{current_graph.classification} is legal only for an active "
                "session, not as a deploy/restart fallback"
            ),
        ))
    if preferred_graph and preferred_graph.issues:
        issues.extend(preferred_graph.issues)
    if staged_graph and staged_graph.issues:
        issues.extend(staged_graph.issues)
    if not staged_path:
        # Third outcome (issue #2135): there is NO staged graph at all — the
        # household declared a roleful topology and paused before crossover
        # preview. Park the speaker silent rather than refuse, so the box can
        # still take deploys while it sits in that limbo.
        #
        # Deliberately gated on "no staged locator", not on "no usable staged
        # graph": a staged graph that EXISTS but fails its safety proof keeps
        # blocking with its blockers below. That is a commissioning bug, not a
        # paused household, and papering over it with silence would hide it.
        #
        # This branch is also LAST on purpose — every real graph above (approved
        # runtime, applied baseline, staged all-muted) has already been
        # considered, so a parked file can never shadow a graph that carries
        # actual driver protection. Recovery needs no operator action: the
        # moment commissioning stages a startup graph, `select_active_startup`
        # above wins on the next root hardware-reconcile/deploy convergence pass.
        parked_text, parked_graph = build_parked_muted_graph(
            topology, config_path=parked_config_path
        )
        if parked_text is not None and parked_graph.allowed:
            # No `event=` line here: this function is a pure decision and is
            # also reached by read-only callers. The stable
            # `event=active_speaker.runtime_graph decision=parked_muted` line is
            # emitted by `apply_safe_graph_decision_to_statefile`, on every apply
            # that resolves to parked — including one that finds the statefile
            # already pointing at the parked config and writes nothing.
            selected = str(parked_muted_config_path(parked_config_path))
            return SafeGraphDecision(
                status=PARKED_MUTED_STATUS,
                selected_config_path=selected,
                reason=PARKED_MUTED_REASON,
                topology_contract=contract,
                current_graph=current_graph,
                preferred_graph=preferred_graph,
                fallback_graph=parked_graph,
                # Proceeding is not the same as being clean (#2145). Parking is
                # now reachable for a topology that still carries blockers, so
                # the decision REPORTS them: `ok` stays True (it is derived from
                # `status`, never from `issues`), the deploy continues, and the
                # household still sees each blocker in the install transcript.
                # Deliberately `contract.issues` alone — exactly the set the
                # parked verdict declined to refuse on — rather than the wider
                # `issues` list above, which also collects "no candidate at this
                # path" noise from graphs a parked box is EXPECTED not to have.
                # On a blocker-free topology this is empty, so the clean parked
                # decision is unchanged.
                issues=tuple(contract.issues),
            )
        issues.append(_issue(
            "blocker",
            "active_startup_graph_missing",
            (
                "saved topology has roleful/protected outputs but no staged "
                "all-muted active startup graph is available"
            ),
        ))
        # Deduped: the parked verifier re-runs `classify_camilla_graph`, which
        # prepends the SAME `contract.issues` the current/preferred/staged
        # classifications already contributed above. Appending them verbatim
        # printed each topology-level blocker twice in the install transcript.
        seen = {(issue["code"], issue["message"]) for issue in issues}
        issues.extend(
            issue
            for issue in parked_graph.issues
            if (issue["code"], issue["message"]) not in seen
        )
    return SafeGraphDecision(
        status="blocked",
        selected_config_path=None,
        reason=(
            "roleful/protected topology has no legal all-muted active startup graph"
        ),
        topology_contract=contract,
        current_graph=current_graph,
        preferred_graph=preferred_graph,
        fallback_graph=staged_graph,
        issues=tuple(issues),
    )


def write_camilla_statefile(
    statefile_path: str | Path,
    config_path: str | Path,
    *,
    channel_slots: int = 5,
) -> None:
    """Write CamillaDSP's persisted config path with muted volume slots.

    Published atomically: a reader sees the old statefile or the complete new
    one, never a partial write. A truncated ``outputd-statefile.yml`` is a
    CamillaDSP that cannot start — the same class of dead box #2664 is about —
    and since that issue this writer also runs during install, on the deploy
    path, which is exactly when a power cut or an OOM kill is most likely.
    """

    target = Path(statefile_path)
    slots = max(1, int(channel_slots))
    payload: dict[str, Any] = {}
    try:
        existing = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        existing = None
    if isinstance(existing, dict):
        payload.update(existing)
    payload["config_path"] = str(config_path)
    if "mute" not in payload:
        payload["mute"] = [False] * slots
    if "volume" not in payload:
        payload["volume"] = [0.0] * slots
    ordered = {"config_path": payload.pop("config_path")}
    ordered.update(payload)
    atomic_write_text(target, yaml.safe_dump(ordered, sort_keys=False), mode=0o644)


def apply_safe_graph_decision_to_statefile(
    decision: SafeGraphDecision,
    *,
    statefile_path: str | Path = DEFAULT_CAMILLA_STATEFILE,
    topology: OutputTopology | None = None,
) -> bool:
    """Persist the selected graph if the statefile is absent or needs repair.

    ``topology`` is used only by the PARKED branch: unlike every other selectable
    graph, the parked graph is generated rather than found, so this writer
    materialises it from the saved topology and RE-PROVES it all-muted before the
    bytes reach disk. Re-deriving (instead of carrying decision-time bytes) means
    the write-time proof is a real second check, not a replay of the first.
    """

    if not decision.ok or not decision.selected_config_path:
        return False
    materialise_safe_graph_decision(decision, topology=topology)
    if decision.status == PARKED_MUTED_STATUS:
        # Logged HERE, not at decision time: the decision function is also
        # reached by read-only callers (`runtime-safe-graph` without
        # --write-statefile, the correction reset probe, the multiroom follower's
        # restore candidates), and a `decision=parked_muted` line from those
        # would read as "the box was just parked" when nothing was written.
        #
        # It DOES fire on a statefile no-op (the `_path_matches` return below),
        # and that is deliberate: `_materialise_parked_muted_config` above has
        # already RE-PROVED the parked graph all-muted by this point, so real
        # work happened. (Re-proved, not rewritten — that function returns early
        # without touching the file when the on-disk bytes already match.)
        # The line means "this apply resolved to parked", not
        # "the statefile changed" — moving it below the compare would silence a
        # parked box on every deploy after the first.
        log_event(
            logger,
            "active_speaker.runtime_graph",
            decision=PARKED_MUTED_STATUS,
            reason=decision.reason,
            topology_mode=decision.topology_contract.classification,
            statefile=str(statefile_path),
            config_path=decision.selected_config_path,
        )
    current = _statefile_config_path(statefile_path)
    if _path_matches(current, decision.selected_config_path):
        return False
    write_camilla_statefile(statefile_path, decision.selected_config_path)
    return True


def materialise_safe_graph_decision(
    decision: SafeGraphDecision,
    *,
    topology: OutputTopology | None = None,
) -> None:
    """Materialise any generated graph without taking statefile ownership.

    Park-before-save runs in jasper-web, where the generated-config directory
    is writable but CamillaDSP's root-owned statefile is not.  The websocket
    load persists that pointer on behalf of the daemon.  Root boot/reconcile
    callers compose this helper with :func:`write_camilla_statefile` through
    :func:`apply_safe_graph_decision_to_statefile`.
    """

    if (
        decision.ok
        and decision.selected_config_path
        and decision.status == PARKED_MUTED_STATUS
    ):
        _materialise_parked_muted_config(
            decision.selected_config_path,
            topology=topology,
        )


def _materialise_parked_muted_config(
    config_path: str | Path,
    *,
    topology: OutputTopology | None,
) -> None:
    """Write the parked graph to disk, refusing anything not proved all-muted.

    Runs on every apply, not only when the statefile changes: the statefile may
    already point here while the config itself is missing — a deleted or
    never-written generated-config dir — and a statefile pointing at a missing
    config is how CamillaDSP fails to start. It is a NO-OP when the on-disk bytes
    already match, so the steady state costs one read instead of a new inode plus
    a ``camilladsp --check`` subprocess on every deploy (twice: outputd's
    statefile and camilla#2's).
    """

    import tempfile

    from jasper.active_speaker.profile import ActiveSpeakerConfigError
    from jasper.dsp_apply import validate_camilla_config

    topology = topology or load_output_topology_strict()
    text, graph = build_parked_muted_graph(topology, config_path=config_path)
    if text is None or not graph.allowed:
        raise ActiveSpeakerConfigError(
            "refusing to write a parked active-speaker graph that is not "
            "proved all-muted: "
            + "; ".join(issue["code"] for issue in graph.issues)
        )
    target = Path(config_path)
    try:
        if target.read_text(encoding="utf-8") == text:
            return
    except (OSError, UnicodeError):
        pass  # absent, unreadable, or not text — fall through and rewrite
    target.parent.mkdir(parents=True, exist_ok=True)
    # CamillaDSP preflight before these bytes become the box's boot graph. An
    # unloadable parked config would crash-loop jasper-camilla, which is a worse
    # outcome than the blocked deploy this whole path replaces — so a rejected
    # graph degrades back to blocked rather than shipping. Checked on a temp
    # sibling so a rejected graph never lands on the real name. The name is
    # per-invocation unique (mkstemp) rather than a fixed dotfile: two writers in
    # this shared dir — install's outputd and camilla#2 passes, or a concurrent
    # web flow — would otherwise unlink each other's probe mid-validation. A
    # missing camilladsp binary (dev host, CI) passes through, the same
    # `ok_to_apply` contract protected staging uses.
    handle, probe_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.check-", suffix=".yml"
    )
    os.close(handle)
    probe = Path(probe_name)
    try:
        atomic_write_text(probe, text, mode=0o640)
        validation = validate_camilla_config(probe)
    finally:
        probe.unlink(missing_ok=True)
    if not validation.ok_to_apply:
        raise ActiveSpeakerConfigError(
            "generated parked active-speaker graph failed CamillaDSP "
            f"validation ({validation.status.value}): {validation.error}"
        )
    atomic_write_text(target, text, mode=0o640)
