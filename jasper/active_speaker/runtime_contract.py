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

import math
import os
import json
import hashlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterable, Literal, Mapping

import yaml

from jasper.audio_measurement.evidence_identity import NormalizedActiveRawIdentity

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
from .camilla_yaml import (
    BASELINE_LIMITER_CLIP_LIMIT_DB,
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
    float_value as _float_value,
    mains_highpass_present,
    pipeline_contains_chain,
    sub_audible_guard_present,
    sub_guard_present,
    truthy_bool as _truthy_bool,
    tweeter_guard_present,
    view_from_yaml_dict,
)
from .environment import (
    CAMILLA_CLASS_PROGRAM_BAKE,
    DEFAULT_CAMILLA_STATEFILE,
    classify_camilla_config_text,
    parse_camilla_statefile_config_path,
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

DEFAULT_FLAT_OUTPUTD_CONFIG = Path("/etc/camilladsp/outputd-cutover.yml")
# The ``shm_ring`` sibling of the flat outputd cutover config. A ring-armed box
# (JASPER_FANIN_CAMILLA_COUPLING=shm_ring) MUST re-seed its statefile to this ring
# config on a camilla restart/deploy, not to the loopback flat config above — that
# revert is audit finding 5's "built-in revert" (a hand-placed ring config that any
# camilla restart silently reverts to loopback). Emitted by
# jasper.sound.camilla_yaml.emit_flat_ring_config; installed by install.sh next to
# outputd-cutover.yml. The graph selector picks between the two by the persisted
# coupling (see ``safe_graph_for_current_topology``'s ``coupling`` argument).
DEFAULT_RING_FLAT_OUTPUTD_CONFIG = Path("/etc/camilladsp/outputd-cutover-ring.yml")
DEFAULT_LEGACY_FLAT_CONFIG = Path("/etc/camilladsp/v1.yml")
DEFAULT_CAMILLA2_STATEFILE = Path("/var/lib/camilladsp/crossover-statefile.yml")

GRAPH_FLAT_FULL_RANGE = "flat_full_range"
GRAPH_ALL_MUTED_ACTIVE_STARTUP = "all_muted_active_startup"
GRAPH_GUARDED_COMMISSIONING = "guarded_commissioning"
GRAPH_APPROVED_ACTIVE_RUNTIME = "approved_active_runtime"
GRAPH_DRIVER_DOMAIN_BASELINE = "driver_domain_baseline"
# The active-leader's camilla#1 program bake: a flat (no-Layer-A) program graph
# whose playback is a File/pipe sink, not a DAC. Allowed regardless of topology
# (safe by construction — no DAC, no driver to over-drive); see
# _flat_graph_allowed and docs/HANDOFF-distributed-active.md "camilla#1 program
# bake — verifier exemption".
GRAPH_PROGRAM_BAKE_PIPE = "program_bake_pipe"
GRAPH_UNKNOWN = "unknown"
GRAPH_UNSAFE = "unsafe"

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

OUTPUTD_ACTIVE_PLAYBACK_DEVICE = "outputd_active_content_playback"
OUTPUTD_ENDPOINT_GRAPH_CLASSIFICATIONS = frozenset((
    GRAPH_ALL_MUTED_ACTIVE_STARTUP,
    GRAPH_GUARDED_COMMISSIONING,
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    GRAPH_DRIVER_DOMAIN_BASELINE,
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
    ok: bool
    width: int | None
    reason: str
    source: str | None = None
    primary_graph: GraphSafety | None = None
    endpoint_graph: GraphSafety | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "width": self.width,
            "reason": self.reason,
            "source": self.source,
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


def active_topology_requires_roleful_graph(topology: OutputTopology) -> bool:
    return classify_output_contract(topology).requires_roleful_graph


def topology_supports_shm_ring(topology: OutputTopology) -> bool:
    """True iff the saved topology can be driven by the ``shm_ring`` coupling.

    The topology-contract citizenship for rings (audio-graph consolidation P2):
    Ring A/Ring B carry a full-range **stereo** program on a single coherent ALSA
    sink. So ring is legal ONLY for the plain stereo full-range contract, and
    NOT for:

    - roleful / protected / subwoofer topologies (``requires_roleful_graph`` — the
      ring is stereo-pinned and cannot carry a per-driver crossover; the Rust
      outputd config.rs likewise requires a full-range stereo L/R sink for
      ``shm_ring``, and the statefile seeder sends these to the driver-domain graph,
      never the ring flat config);
    - composite sinks (dual-Apple — TWO+ ``hardware.child_devices``): the ring
      ioplug is a single coherent 2-ch device, not the 4-ch composite the two
      child DACs need. That is P8's ring-v2 (N-channel) problem. The exclusion is
      keyed on ``len(child_devices) >= 2`` — a *plurality* of child DACs, each its
      own USB clock domain (``dac.py``'s only ``kind="composite"`` profile is the
      dual-Apple 4-ch, ``child_profile_ids=(apple, apple)``). A *single* child
      (``len == 1``) is the opposite: one coherent stereo sink on one clock, which
      IS exactly what the ring drives — the single-Apple-dongle and single
      registered DAC (hifiberry) paths both populate ``child_devices=(card,)`` for
      stable serial identity, and that single entry must NOT disqualify the ring.
      (Pre-2026-07 this read ``if child_devices:`` — a bare truthiness check that
      wrongly refused every shipped-default box, since observed hardware always
      records its one child. See DEFECT 2.)

    An UNCONFIGURED topology (no speaker groups — the common fresh-install shape)
    IS ring-eligible: it uses the flat stereo graph, exactly the shape ring
    replaces. This predicate is what the default-coupling resolver, multiroom
    bond-formation prechecks, and the reconciler consult; DEFAULT coupling may
    resolve to ``shm_ring`` only when this predicate and the ring arm preflights
    pass, otherwise it remains loopback."""
    contract = classify_output_contract(topology)
    if contract.requires_roleful_graph:
        return False
    # Composite (dual-Apple, kind="composite") is excluded even when nominally
    # stereo: a MULTI-child sink spans >1 USB clock domain and is not the single
    # coherent L/R sink the ring drives. A single child (len == 1) IS that coherent
    # sink — the shipped-default dongle/hifiberry path records one child for stable
    # identity — so gate on a plurality of children, never bare truthiness.
    if len(topology.hardware.child_devices) >= 2:
        return False
    # Stereo full-range OR unconfigured (flat stereo fallback) are the ring-legal
    # shapes; an explicit mono full-range cannot be driven by a stereo ring.
    return contract.classification in (
        CONTRACT_NORMAL_STEREO_FULL_RANGE,
        CONTRACT_UNCONFIGURED,
    )


def _protected_tweeter_outputs(contract: OutputContract) -> set[int]:
    """Physical output indices the saved topology assigns to a protected tweeter."""
    return {
        int(item.physical_output_index)
        for item in contract.protected_assignments
        if item.role == "tweeter" and item.physical_output_index is not None
    }


def flat_program_graph_blocked_reason(
    topology: OutputTopology | None = None,
) -> str | None:
    """Reason a flat full-range *program* graph must not go live, or ``None``.

    The program lane (``jasper.sound.camilla_yaml.emit_sound_config`` and the
    ``/sound`` / correction callers it backs) emits a 2-channel passthrough with
    no per-driver crossover or protection. That graph is illegal exactly when the
    saved output topology assigns a protected *tweeter* role: full-range program
    would reach a compression-driver tweeter (shrill, and a tweeter-damage risk).
    This is the L0 invariant (docs/HANDOFF-audio-measurement-core.md).

    Returns a household-readable detail string naming the protected output(s)
    when a flat program graph is blocked, or ``None`` when it is safe — the
    common full-range / mono / subwoofer / unconfigured cases, so a non-active
    speaker is unaffected. It is a *topology* predicate, not a graph check: the
    program lane is structurally flat, so the only question is whether the
    topology has a tweeter to protect. (Verifying a graph that *should* be
    protective — an active baseline — is :func:`classify_camilla_graph`'s job.)

    Fail-closed: a corrupt/unreadable saved topology returns a reason (block)
    rather than raising, so a caller can never read "safe" out of a topology it
    could not load. Callers own the policy and the structured logging:
    :class:`jasper.sound.graph_carrier.CarrierCannotHostEq` for ``/sound``,
    ``CorrectionRuntimeSafetyError`` for room correction.
    """
    try:
        contract = classify_output_contract(topology or load_output_topology_strict())
    except OutputTopologyError as exc:
        return f"the saved output topology is unavailable or invalid ({exc})"
    if not _protected_tweeter_outputs(contract):
        return None
    return _protected_output_detail(contract)


def _statefile_config_path(statefile_path: str | Path | None) -> str | None:
    path = Path(statefile_path or os.environ.get("JASPER_CAMILLA_STATEFILE") or DEFAULT_CAMILLA_STATEFILE)
    try:
        return parse_camilla_statefile_config_path(path.read_text(encoding="utf-8"))
    except OSError:
        return None


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


def _flat_graph_allowed(
    contract: OutputContract,
    *,
    config_path: str | None,
    summary: dict[str, Any],
    program_bake_pipe: bool = False,
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
    allowed = not contract.requires_roleful_graph
    playback_channels = summary.get("playback_channels")
    full_range_outputs = {
        item.physical_output_index
        for item in contract.assignments
        if item.role == "full_range" and item.physical_output_index is not None
    }
    if (
        allowed
        and contract.topology_configured
        and isinstance(playback_channels, int)
        and playback_channels > len(full_range_outputs)
    ):
        allowed = False
        issues.append(_issue(
            "blocker",
            "flat_full_range_graph_wider_than_topology",
            (
                f"flat full-range graph exposes {playback_channels} output "
                f"channels, but saved full-range topology assigns only "
                f"{len(full_range_outputs)} physical output(s)"
            ),
        ))
    if not allowed:
        if contract.requires_roleful_graph:
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
        and clip_limit <= 0.0
        and limiter_params.get("soft_clip") is True
    )


def _baseline_output_chain(
    payload: dict[str, Any],
    *,
    assignment: OutputAssignment,
    channel: int,
    bass_management_highpass: bool,
    bass_extension: bool = False,
) -> tuple[tuple[str, str], ...] | None:
    """Prove the exact emitter-owned chain before the canonical limiter."""

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
            crossovers = _baseline_output_chain(
                payload,
                assignment=assignment,
                channel=index,
                bass_management_highpass=(
                    contract.subwoofer_present and index in mains_low_outputs
                ),
                bass_extension=index in bass_owner_channels,
            )
            if crossovers is None:
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
    if staged_dependent and (not staged_path or not config_path):
        issues.append(_issue(
            "blocker",
            "active_staged_metadata_missing",
            "guarded active graphs require a staged locator and graph path",
        ))
    if (
        classification == GRAPH_ALL_MUTED_ACTIVE_STARTUP
        and staged_path
        and config_path
        and not _path_matches(config_path, staged_path)
    ):
        issues.append(_issue(
            "blocker",
            "active_staged_locator_mismatch",
            "all-muted active startup path does not match staged metadata",
        ))
    if staged_dependent and staged_path and config_path and not staged_match:
        issues.append(_issue(
            "blocker",
            "active_staged_metadata_mismatch",
            "guarded active graph no longer matches saved topology metadata",
        ))
    if staged_dependent and staged_path and config_path and not staged_guard_ready:
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
    issues: list[dict[str, str]] = list(contract.issues)
    path_s = str(config_path) if config_path is not None else None
    if text is None:
        return GraphSafety(
            classification=GRAPH_UNKNOWN,
            allowed=False,
            config_path=path_s,
            issues=tuple(issues) or (
                _issue("blocker", "camilla_graph_missing", "no CamillaDSP graph was provided"),
            ),
        )

    summary = classify_camilla_config_text(text)
    for issue in summary.get("issues", []):
        if isinstance(issue, dict):
            issues.append(_issue(
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
        graph = _flat_graph_allowed(
            contract,
            config_path=path_s,
            summary=summary,
            program_bake_pipe=program_bake_pipe,
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

    if issues:
        return GraphSafety(
            classification=graph.classification,
            allowed=False,
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
    except (ValueError, yaml.YAMLError):
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
    raw = text.encode("utf-8")
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
        or config.get("predecessor_sha256")
        != hashlib.sha256(predecessor_graph.encode("utf-8")).hexdigest()
        or config.get("desired_sha256")
        != hashlib.sha256(desired_graph.encode("utf-8")).hexdigest()
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
    desired_profile: "BassExtensionProfile | None" = None,
) -> GraphSafety:
    """Canonical synchronous graph/evidence boundary."""

    if evidence_source == "desired":
        if (
            any(path is not None for path in (
                statefile_path, candidate_path, applied_baseline_path,
                profile_path, intent_path, staged_metadata_path,
            ))
            or candidate_kind is not None
            or not isinstance(graph_text, str)
            or not isinstance(applied_baseline_state, Mapping)
            or desired_profile is None
        ):
            return _unsafe_boundary("bass_extension_source_invalid", "desired evidence is incomplete")
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
        or desired_profile is not None
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
        except (OSError, UnicodeError):
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


async def classify_active_bass_extension_graph(
    topology: OutputTopology,
    *,
    statefile_path: Path,
    read_active_graph_text: Callable[[], Awaitable[str | None]],
    applied_baseline_path: Path,
    profile_path: Path,
    intent_path: Path,
    staged_metadata_path: Path,
) -> GraphSafety:
    """Canonical live-active boundary with readback inside the sandwich."""

    for _attempt in range(2):
        try:
            applied1 = _read_optional_bytes(applied_baseline_path)
            intent1 = _read_optional_bytes(intent_path)
            profile1 = _read_optional_bytes(profile_path)
            staged1 = _read_optional_bytes(staged_metadata_path)
            selector1 = statefile_path.read_bytes()
            selected1_s = parse_camilla_statefile_config_path(selector1.decode("utf-8"))
            if not selected1_s:
                continue
            selected_path = Path(selected1_s)
            selected1 = selected_path.read_bytes()
            active_text = await read_active_graph_text()
            selected2 = selected_path.read_bytes()
            selector2 = statefile_path.read_bytes()
            selected2_s = parse_camilla_statefile_config_path(selector2.decode("utf-8"))
            staged2 = _read_optional_bytes(staged_metadata_path)
            profile2 = _read_optional_bytes(profile_path)
            intent2 = _read_optional_bytes(intent_path)
            applied2 = _read_optional_bytes(applied_baseline_path)
        except Exception:  # noqa: BLE001 - callback errors fail closed
            continue
        if (
            not isinstance(active_text, str)
            or selected2_s != str(selected_path)
            or not all((
                applied1 == applied2,
                intent1 == intent2,
                profile1 == profile2,
                staged1 == staged2,
                selected1 == selected2,
            ))
        ):
            continue
        try:
            selected_text = selected1.decode("utf-8")
        except UnicodeError:
            continue
        if (
            _normalized_graph_fingerprint(active_text) is None
            or _normalized_graph_fingerprint(active_text)
            != _normalized_graph_fingerprint(selected_text)
        ):
            continue
        return _classify_bass_extension_snapshot(
            topology,
            graph_text=active_text,
            config_path=str(selected_path),
            applied_baseline_bytes=applied1,
            applied_baseline_state=None,
            profile_bytes=profile1,
            intent_bytes=intent1,
            staged_metadata_bytes=staged1,
        )
    return _unsafe_boundary("bass_extension_active_snapshot_unstable", "live graph authority could not be proved")


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
) -> tuple[int | None, str | None]:
    if not graph.allowed:
        issue = graph.issues[0]["code"] if graph.issues else graph.classification
        return None, f"active_graph_unsafe:{issue}"
    if graph.classification not in classifications:
        return None, f"active_graph_not_outputd_endpoint:{graph.classification}"
    if graph.playback_device != OUTPUTD_ACTIVE_PLAYBACK_DEVICE:
        return None, "active_outputd_lane_missing"

    got = int(graph.playback_channels or 0)
    if got < 2 or got > cap_channels:
        return None, f"active_graph_width_out_of_range got={got} cap={cap_channels}"
    return got, None


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
    width, problem = _outputd_endpoint_width(primary_graph, cap)
    if width is not None:
        return OutputdActiveLaneDecision(
            ok=True,
            width=width,
            reason="active_outputd_endpoint",
            source="primary_statefile",
            primary_graph=primary_graph,
            endpoint_graph=primary_graph,
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

    width, problem = _outputd_endpoint_width(
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
    )


def safe_graph_for_current_topology(
    topology: OutputTopology | None = None,
    *,
    statefile_path: str | Path | None = None,
    current_config_path: str | Path | None = None,
    preferred_config_path: str | Path | None = None,
    flat_config_path: str | Path = DEFAULT_FLAT_OUTPUTD_CONFIG,
    ring_flat_config_path: str | Path = DEFAULT_RING_FLAT_OUTPUTD_CONFIG,
    coupling: str | None = None,
    applied_baseline_path: str | Path | None = None,
    profile_path: str | Path | None = None,
    intent_path: str | Path | None = None,
    staged_metadata_path: str | Path | None = None,
    consider_applied_baseline: bool = True,
    staged_config: Mapping[str, Any] | None = None,
) -> SafeGraphDecision:
    """Select the only safe persisted CamillaDSP graph for this topology.

    ``coupling`` is the persisted fan-in -> CamillaDSP coupling
    (``JASPER_FANIN_CAMILLA_COUPLING``). When it resolves to ``shm_ring`` AND the
    topology takes the non-roleful flat fallback, the selected flat graph is the
    RING flat config (``ring_flat_config_path``) instead of the loopback
    ``flat_config_path`` — so a ring-armed box's deploy / camilla restart re-seeds
    a ring config instead of reverting to loopback (audit finding 5). A ``None``
    or non-ring coupling preserves the loopback flat selection byte-for-byte. Only
    the flat (stereo/passive) branch is ring-aware; roleful/active topologies are
    P8's ring-v2 concern and always take the driver-domain path here."""

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
    current_path = (
        str(current_config_path)
        if current_config_path
        else _statefile_config_path(statefile)
    )
    if current_config_path:
        current_graph = classify_bass_extension_graph(
            topology,
            evidence_source="persisted_candidate",
            candidate_kind="explicit",
            candidate_path=Path(current_config_path),
            **authority,
        )
    elif current_path:
        current_graph = classify_bass_extension_graph(
            topology,
            evidence_source="persisted_boot",
            statefile_path=statefile,
            **authority,
        )
    else:
        current_graph = None
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
        and not contract.requires_roleful_graph
        # A program-bake pipe is allowed by the verifier (no DAC, no driver to
        # over-drive) but is NOT a selectable solo graph: its File sink feeds the
        # snapserver FIFO, not the DAC, so preserving it on a solo speaker would
        # leave the DAC silent. Selecting/wiring camilla#1 is a later Stage-B
        # slice; this selector must never pick the pipe bake as a speaker's own
        # output graph.
        and current_graph.classification != GRAPH_PROGRAM_BAKE_PIPE
    ):
        return SafeGraphDecision(
            status="preserve_current",
            selected_config_path=current_path,
            reason="current CamillaDSP graph is legal for saved topology",
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
        )
    if (
        current_graph
        and current_graph.allowed
        and current_graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME
    ):
        return SafeGraphDecision(
            status="preserve_current",
            selected_config_path=current_path,
            reason="current approved active-speaker runtime graph is legal for saved topology",
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
        )
    if (
        preferred_graph
        and preferred_graph.allowed
        and preferred_graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME
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

    if not contract.requires_roleful_graph:
        # Ring-armed box: re-seed the RING flat config, not the loopback one, so a
        # camilla restart/deploy keeps the rings (audit finding 5). Fail-SAFE: the
        # loopback flat config below is the fallback whenever the ring path is not
        # taken — it does NOT restore audio on an armed box (outputd still reads
        # Ring B), but it prevents a camilla crash-loop and keeps the box
        # doctor-visible so the operator can disarm; the coupling reconciler's own
        # activation gate is where a truly unarm-able ring is caught. A non-ring
        # coupling skips the ring path entirely (loopback flat, byte-for-byte).
        #
        # Gate the ring path on the FULL topology-ring-eligibility predicate
        # (topology_supports_shm_ring), not just `not requires_roleful_graph`: the
        # predicate additionally excludes composite (dual-Apple child_devices) and
        # explicit-mono topologies, which are NOT ring-legal (a stereo ring cannot
        # drive a 4-ch composite sink — that is P8's ring-v2). Without this, a
        # composite box carrying a stale coupling=shm_ring would seed a stereo-ring
        # flat config it cannot play. This is the promised "seeder consults the
        # predicate" consultation.
        from jasper.fanin_coupling import COUPLING_SHM_RING, resolve_coupling

        if resolve_coupling(coupling) == COUPLING_SHM_RING and (
            topology_supports_shm_ring(topology)
        ):
            ring_fallback = classify_bass_extension_graph(
                topology,
                evidence_source="persisted_candidate",
                candidate_kind="explicit",
                candidate_path=Path(ring_flat_config_path),
                **authority,
            )
            if ring_fallback.allowed:
                return SafeGraphDecision(
                    status="select_flat",
                    selected_config_path=str(ring_flat_config_path),
                    reason=(
                        "saved topology has no roleful/protected outputs and is "
                        "ring-eligible (stereo/unconfigured); box is ring-armed "
                        "(coupling=shm_ring)"
                    ),
                    topology_contract=contract,
                    current_graph=current_graph,
                    preferred_graph=preferred_graph,
                    fallback_graph=ring_fallback,
                )
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
                reason="saved topology has no roleful/protected outputs",
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
        issues.append(_issue(
            "blocker",
            "active_startup_graph_missing",
            (
                "saved topology has roleful/protected outputs but no staged "
                "all-muted active startup graph is available"
            ),
        ))
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
    """Write CamillaDSP's persisted config path with muted volume slots."""

    target = Path(statefile_path)
    target.parent.mkdir(parents=True, exist_ok=True)
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
    target.write_text(yaml.safe_dump(ordered, sort_keys=False), encoding="utf-8")
    os.chmod(target, 0o644)


def apply_safe_graph_decision_to_statefile(
    decision: SafeGraphDecision,
    *,
    statefile_path: str | Path = DEFAULT_CAMILLA_STATEFILE,
) -> bool:
    """Persist the selected graph if the statefile is absent or needs repair."""

    if not decision.ok or not decision.selected_config_path:
        return False
    current = _statefile_config_path(statefile_path)
    if _path_matches(current, decision.selected_config_path):
        return False
    write_camilla_statefile(statefile_path, decision.selected_config_path)
    return True
