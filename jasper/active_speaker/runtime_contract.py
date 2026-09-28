# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Runtime safety contract for roleful active-speaker CamillaDSP graphs.

One graph this module must reject (see `sound.flat_verifier`): a flat
graph that maps full-range stereo directly to DAC outputs is illegal
when the saved output topology assigns any physical output to a
tweeter/protected role.

``jasper.output_topology`` owns the declarative physical-output contract and
:mod:`jasper.active_speaker.output_contract` classifies it. This module owns
the runtime question that follows from it: whether a candidate or running
CamillaDSP graph is legal for that exact saved topology, and which graph
install/reconcile paths may select when they need a safe fallback. It is
deliberately file-based and side-effect-free except for the explicit
statefile writer helper at the bottom.
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import (
    Any, Awaitable, Callable, Collection, Literal, Mapping,
)

import yaml

from jasper.atomic_io import atomic_write_text
from jasper.audio_measurement.evidence_identity import NormalizedActiveRawIdentity
from jasper.bass_extension.dynamic import validate_dynamic_bass_descriptor
from jasper.bass_extension.dynamic_graph import dynamic_bass_owner_groups, validated_base_graph
from jasper.json_fields import issue as _issue
from jasper.log_event import log_event
from jasper.sound.flat_verifier import (
    _flat_graph_allowed,
    _flat_hard_muted_outputs,
    _flat_mono_fold_proved,
    _playback_is_program_bake_pipe,
    _required_mono_fold_output,
)

from jasper.output_topology import (
    OutputTopology,
    OutputTopologyError,
)
from jasper.output_topology_store import load_output_topology_strict, stamp_statefile_topology
from jasper import paths
from jasper.active_speaker.playback_route import ActiveLaneCapabilityGap, active_lane_capability_gap
from jasper.active_speaker.profile import ActiveSpeakerConfigError
from jasper.active_speaker.state_paths import baseline_profile_state_path

from .camilla_yaml import PARKED_CONFIG_NAME, _reserialize_keeping_header
from .camilla_names import STARTUP_MUTE_GAIN_DB, output_commission_mute_name as _commission_mute_name
from .graph.active_verifier import LINEARIZATION_HEADROOM_UNPROVEN_CODE, _active_graph_evidence
from .graph_types import (
    GRAPH_ALL_MUTED_ACTIVE_STARTUP,
    GRAPH_GUARDED_COMMISSIONING,
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    GRAPH_DRIVER_DOMAIN_BASELINE,
    GRAPH_PROGRAM_BAKE_PIPE,
    GRAPH_PARKED_ALL_MUTED,
    GRAPH_UNKNOWN,
    GRAPH_UNSAFE,
    GraphSafety,
)
from .graph_safety import (
    output_hard_muted_and_wired,
    view_from_yaml_dict,
)
from .environment import (
    CAMILLA_CLASS_ACTIVE_PARKED,
    CAMILLA_CLASS_PROGRAM_BAKE,
    classify_camilla_config_text,
    parse_camilla_statefile_config_path,
    read_camilla_statefile_config_path,
)
from .output_contract import (
    ACTIVE_BASELINE_SOURCE,
    ACTIVE_DRIVER_DOMAIN_SOURCE,
    CONTRACT_UNCONFIGURED,
    OutputContract,
    classify_output_contract,
    flat_graph_program_dest_map,
    mains_lowest_driver_indexes as _mains_lowest_driver_indexes,
    subwoofer_output_indexes as _subwoofer_output_indexes,
    topology_allows_flat_dac_graph,
)
from .path_safety import (
    software_guard_ready_for_startup,
    staged_target_signature,
    topology_target_signature,
)

logger = logging.getLogger(__name__)

# The ONE flat outputd startup graph. It is a RING graph: the ring is the only
# transport (ADR-0100), so there is no sibling to re-seed instead of.
DEFAULT_FLAT_OUTPUTD_CONFIG = Path("/etc/camilladsp/outputd-cutover.yml")

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
    "reset output setup at /sound/speaker/, then choose an explicit passive "
    "layout (passive sends full-range to every output and requires a built-in "
    "passive crossover), "
    "or attach an active-capable DAC"
)


def parked_muted_exits(topology: OutputTopology | None = None) -> str:
    """The exits out of parked that are actually reachable on this hardware.

    Fail-soft: any unreadable topology falls back to the general pair rather
    than raising inside a reporting surface.
    """

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




def _path_matches(left: str | Path | None, right: str | Path | None) -> bool:
    if not left or not right:
        return False
    try:
        return Path(left).expanduser().resolve(strict=False) == Path(right).expanduser().resolve(strict=False)
    except OSError:
        return str(left) == str(right)


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
        staged_target_signature(staged_config)
        == topology_target_signature(topology),
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
    rear_calibration: Mapping[str, Any] | None,
    excited_target_ids: Collection[str] = (),
) -> GraphSafety:
    evidence = _active_graph_evidence(
        text, contract, summary, bass_profile_summary, rear_calibration,
        excited_target_ids=excited_target_ids,
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

    A parked graph is accepted because this function CHECKS that it is silent,
    never because verification is skipped for a trusted filename or marker. Four
    structural facts, all read off the parsed graph:

    1. ``devices.playback.type`` is ``File`` — no DAC attached, so no driver can
       be over-driven whatever the topology says.
    2. The pipeline is **exhaustively** the parked shape: one leading ``Mixer``
       step, then exactly ``width`` ``Filter`` steps, step *i* targeting channel
       *i* alone with ``names`` equal to that channel's mute filter. No extra
       step, no extra name inside a step, no reordering.
    3. Every playback channel's mute is a real hard mute — a ``Gain`` at
       ``STARTUP_MUTE_GAIN_DB`` with ``mute: true``.
    4. The playback width covers every physical output the topology assigns, so
       no declared driver sits outside the muted set.

    Fact 2 must be exhaustive because fact 1 bounds the damage but does not
    replace fact 3: a graph could be repointed at a DAC by a later edit while
    the pipeline stayed generous. Requiring only that a mute be present
    SOMEWHERE in each chain admits a ``+240 dB`` ``Gain`` appended as a fourth
    step, the same gain injected into an existing mute step's ``names`` (filters
    apply in order, so a gain after the mute re-amplifies), and an appended
    ``Dither`` step, which generates signal into a muted channel.

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
    rear_calibration: Mapping[str, Any] | None = None,
    excited_target_ids: Collection[str] = (),
) -> GraphSafety:
    """Return whether a CamillaDSP graph is legal for the saved topology."""

    topology = topology or load_output_topology_strict()
    contract = classify_output_contract(topology)
    # The two issue sources are kept APART because the tail below gates the
    # PARKED verdict on only one: ``topology_issues`` describe the saved speaker
    # LAYOUT, ``graph_issues`` the CamillaDSP config TEXT in hand. Merged, a
    # topology blocker refuses a graph it cannot make unsafe.
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
            # The camilla#1 program bake is also a flat (no-Layer-A) program
            # graph, and reaches the flat path so the File-sink exemption in
            # _flat_graph_allowed can clear it regardless of topology.
            CAMILLA_CLASS_PROGRAM_BAKE,
        }
        or path_name == "outputd-cutover.yml"
    )
    if is_flat:
        # Detect the File/pipe playback ONCE here (this scope has the config
        # text) so _flat_graph_allowed stays text-free; the exemption keys
        # strictly on the File-pipe sink.
        program_bake_pipe = _playback_is_program_bake_pipe(text)
        # Same text-free split for the mute set and the mono fold: the topology
        # says whether a fold is owed, the text says whether it is there.
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
            rear_calibration=rear_calibration,
            excited_target_ids=excited_target_ids,
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
        # A PROVED-PARKED graph is refused only by its OWN blockers.
        #
        # Every other classification is refused whenever anything is wrong,
        # because every other graph drives the DAC: against a half-assigned
        # topology it can send the wrong band to a tweeter. The parked graph
        # cannot — its safety is STRUCTURAL and was just proved by
        # `_parked_graph_allowed` against the graph's own bytes.
        #
        # The load-bearing pair is the pipeline exactness plus the hard mutes,
        # NOT the File sink alone: a File sink can feed outputd's pipe, and
        # outputd drives the DAC. None of those facts can be falsified by a
        # topology blocker, so refusing on one only prevented the box from
        # parking.
        #
        # Keyed on the VERDICT, not on the claimed input class, so a graph that
        # merely CLAIMS the parked source marker and fails its proof cannot
        # reach the exemption. `graph_issues` still gate — they describe this
        # graph's own text — while `topology_issues` are reported either way, so
        # the deploy proceeds LOUDLY.
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


def _classify_bass_extension_snapshot(
    topology: OutputTopology,
    *,
    graph_text: str,
    config_path: str | None,
    applied_baseline_bytes: bytes | None,
    applied_baseline_state: Mapping[str, Any] | None,
    staged_metadata_bytes: bytes | None,
    excited_target_ids: Collection[str] = (),
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
    snapshot = (applied or {}).get("recomposition_snapshot") or {}
    if not isinstance(snapshot, Mapping):
        return _unsafe_boundary("bass_extension_block_invalid", "saved tune snapshot is invalid")
    descriptor = snapshot.get("bass_extension") or {}
    channels: tuple[int, ...] = ()
    if descriptor:
        try:
            descriptor = validate_dynamic_bass_descriptor(descriptor)
            contract = classify_output_contract(topology)
            channels = tuple(sorted(
                _subwoofer_output_indexes(contract) or _mains_lowest_driver_indexes(contract)
            ))
            # Startup/parked graphs have their own proof and contain no extension.
            source = str(classify_camilla_config_text(graph_text).get("source") or "")
            if source in (ACTIVE_BASELINE_SOURCE, ACTIVE_DRIVER_DOMAIN_SOURCE):
                graph_text = _reserialize_keeping_header(graph_text, validated_base_graph(
                    yaml.safe_load(graph_text), descriptor, channels,
                    owner_groups=dynamic_bass_owner_groups(channels, (
                        (item.speaker_group_id, item.role, item.output_variant, item.physical_output_index)
                        for item in contract.assignments
                    )),
                ))
        except (AttributeError, KeyError, TypeError, ValueError, yaml.YAMLError):
            return _unsafe_boundary("bass_extension_block_invalid", "bass graph differs from the saved tune")
    graph = classify_camilla_graph(
        config_path,
        topology,
        text=graph_text,
        staged_config=staged,
        bass_profile_summary=NO_BASS_EXTENSION_PROFILE_SUMMARY,
        # The rear calibration section travels in the SAME saved snapshot the
        # bass descriptor does, and is the only authority the stage is proved
        # against. Absent, a rear output must be terminally muted.
        rear_calibration=(
            snapshot.get("rear_calibration")
            if isinstance(snapshot.get("rear_calibration"), Mapping)
            else None
        ),
        excited_target_ids=excited_target_ids,
    )
    return replace(
        graph,
        details={
            **graph.details,
            "bass_extension": dict(descriptor),
            "bass_output_channels": list(channels),
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


def prove_desired_graph(
    topology: OutputTopology,
    graph_text: str,
    *,
    snapshot: Mapping[str, Any] | None,
    excited_target_ids: Collection[str] = (),
) -> GraphSafety:
    """The proof a graph this box compiled must pass before it is written or loaded.

    ``snapshot`` is the saved tune's ``recomposition_snapshot``, the one part of
    an applied record the proof reads. :func:`desired_graph_approved` says
    whether the answer lets the graph run."""
    return classify_bass_extension_graph(
        topology, evidence_source="desired", graph_text=graph_text,
        applied_baseline_state={"recomposition_snapshot": snapshot},
        excited_target_ids=excited_target_ids,
    )


def desired_graph_approved(graph: GraphSafety) -> bool:
    return graph.allowed and graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME


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
    staged_metadata_path: Path | None = None,
    excited_target_ids: Collection[str] = (),
) -> GraphSafety:
    """Canonical synchronous graph/evidence boundary.

    ``excited_target_ids`` is a live measurement take's own evidence and is
    honoured on the ``desired`` source ALONE: it travels in memory from the
    admission call that holds the DSP writer lock, never from a saved file a
    tampered statefile could author.
    """

    if evidence_source == "desired":
        if (
            any(path is not None for path in (
                statefile_path, candidate_path, applied_baseline_path,
                staged_metadata_path,
            ))
            or candidate_kind is not None
            or not isinstance(graph_text, str)
            or not isinstance(applied_baseline_state, Mapping)
        ):
            return _unsafe_boundary("bass_extension_source_invalid", "desired evidence is incomplete")
        return _classify_bass_extension_snapshot(
            topology,
            graph_text=graph_text,
            config_path=None,
            applied_baseline_bytes=None,
            applied_baseline_state=applied_baseline_state,
            staged_metadata_bytes=None,
            excited_target_ids=excited_target_ids,
        )

    if (
        graph_text is not None
        or applied_baseline_state is not None
        or applied_baseline_path is None
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
            selected1 = selected_path.read_bytes()
            selected2 = selected_path.read_bytes()
            if evidence_source == "persisted_boot":
                selector2 = statefile_path.read_bytes()
                selected2_s = parse_camilla_statefile_config_path(selector2.decode("utf-8"))
                if selected2_s != str(selected_path):
                    continue
            staged2 = _read_optional_bytes(staged_metadata_path)
            applied2 = _read_optional_bytes(applied_baseline_path)
        except (OSError, UnicodeError, ValueError):
            continue
        if not all((
            applied1 == applied2,
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
            staged_metadata_bytes=staged1,
        )
    return _unsafe_boundary("bass_extension_snapshot_unstable", "graph authority changed while it was read")


async def classify_active_bass_extension_graph(
    topology: OutputTopology,
    *,
    statefile_path: Path,
    read_active_graph_text: Callable[[], Awaitable[str | None]],
    canonicalize_graph_text: Callable[[str], Awaitable[str | None]],
    applied_baseline_path: Path,
    staged_metadata_path: Path,
) -> GraphSafety:
    """Canonical live-active boundary with readback inside the sandwich.

    Both sides of the fingerprint comparison MUST come from CamillaDSP.
    ``read_active_graph_text`` is its own re-serialization of the running graph,
    which default-fills every omitted key; the statefile-selected file is
    JTS-authored text that leaves defaults out, so it goes through CamillaDSP's
    ``ReadConfig`` (``canonicalize_graph_text``) before being fingerprinted.
    Comparing the raw file against the readback can never match and fails
    CLOSED, so the whole gate would silently refuse everything.

    ``canonicalize_graph_text`` is a REQUIRED caller-injected seam: injected so
    CamillaDSP stays the single authority on its own schema, required so a new
    caller cannot re-open the same silent trap by omitting it.
    """

    reason = "live graph authority could not be proved"
    for _attempt in range(2):
        try:
            applied1 = _read_optional_bytes(applied_baseline_path)
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
        # Safe to gather: every caller hands both callables the SAME
        # CamillaController, whose `_call` holds its lock for the whole call, so
        # these serialize rather than interleaving on one websocket.
        active_result, canonical_result = await asyncio.gather(
            read_active_graph_text(),
            canonicalize_graph_text(selected_text),
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
            applied2 = _read_optional_bytes(applied_baseline_path)
        except (OSError, UnicodeError, ValueError):
            reason = "the graph authority could not be re-read"
            continue
        if (
            selected2_s != str(selected_path)
            or not all((
                applied1 == applied2,
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
            staged_metadata_bytes=staged1,
        )
    return _unsafe_boundary("bass_extension_active_snapshot_unstable", reason)


def parked_muted_config_path(path: str | Path | None = None) -> Path:
    """The deterministic on-disk location of the PARKED graph.

    Lives beside the staged startup config in the generated-config dir (staging
    owns that directory constant, so there is one spelling of it).
    """

    from jasper.active_speaker.staging import DEFAULT_CAMILLA_CONFIG_DIR  # lazy: test_runtime_convergence redirects staging.DEFAULT_CAMILLA_CONFIG_DIR

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

    from jasper.active_speaker.camilla_yaml import emit_active_speaker_parked_config  # lazy: test_active_speaker_runtime_contract patches camilla_yaml

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

    Empty whenever none of them failed THAT way, so a caller can ask "did this
    box's own active graph regress on the headroom arithmetic?" without
    re-deriving the condition, and a new refusal reason cannot silently start
    firing a migration guard written for this one.

    DE-DUPLICATED, order-preserving: on a commissioned box the two graphs asked
    here are usually the SAME FILE. Keyed on the whole issue rather than the
    code, so two branches that genuinely both regressed still report one line
    each with their own role and numbers.
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
    applied_baseline_path: str | Path | None = None,
    staged_metadata_path: str | Path | None = None,
    consider_applied_baseline: bool = True,
) -> SafeGraphDecision:
    """Select the only safe persisted CamillaDSP graph for this topology.

    The flat branch selects one PRE-RENDERED file, because a flat graph is the
    same on every box. A roleful box's graph is per-speaker, so there is nothing
    to pick between: the applied baseline and the staged all-muted startup are
    emitted by the commissioning path and the roleful branches below preserve or
    select whatever the box's own graphs declare.

    An armed roleful box's residual is therefore a STALE ARTIFACT, not a wrong
    selection — an on-disk baseline still naming the pre-arm ALSA lane is the
    graph the box has, and ``jasper-active-speaker baseline-reemit --endpoint
    ring`` is what closes it (step ONE of the arm ladder). This function
    deliberately adds no refusal of its own: blocking here would turn a
    recoverable stale artifact into a box that cannot seed a graph at all.

    The PARKED shape needs nothing either way — its ``File`` sink is DAC- and
    transport-agnostic by construction."""

    from jasper.active_speaker.staging import staged_metadata_path as default_staged_path  # lazy: test_ring_active_endpoint patches staging.staged_metadata_path

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
    statefile = paths.camilla_statefile(statefile_path)
    applied_path = Path(applied_baseline_path or baseline_profile_state_path())
    staged_path_authority = Path(staged_metadata_path or default_staged_path())

    authority = {
        "applied_baseline_path": applied_path,
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
        # A program-bake pipe is allowed by the verifier but is NOT a selectable
        # solo graph: its File sink feeds the snapserver FIFO, so preserving it
        # on a solo speaker would leave the DAC silent.
        and current_graph.classification != GRAPH_PROGRAM_BAKE_PIPE
        # The PARKED graph is the same shape of trap: legal for ANY topology, so
        # without this it would be "preserved" forever after a reset. The
        # exclusion lets the selector fall through to `select_flat` once the
        # household saves an explicit passive layout.
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
    # arithmetic must not be quietly re-pointed at the all-muted startup graph.
    # That fall is legal, which is what makes it dangerous: the deploy stays
    # GREEN, the speaker goes SILENT, and it is sticky, because the next deploy
    # preserves the all-muted graph it just selected. Refuse instead, carrying
    # the numbers: the boot writer re-emits the baseline on this refusal, and a
    # human is summoned to `baseline-reemit` only if that fails (#2847).
    #
    # Narrow on purpose: only the NUMERIC refusal fires it. A shape refusal is a
    # different defect with a different remedy, and every other reason a box
    # lands on the staged anchor is a state this ladder is SUPPOSED to resolve
    # silently and green.
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
        # Third outcome: NO staged graph at all — a roleful topology declared and
        # paused before crossover preview. Park the speaker silent rather than
        # refuse, so the box can still take deploys in that limbo.
        #
        # Gated on "no staged LOCATOR", not "no usable staged graph": a staged
        # graph that exists but fails its safety proof keeps blocking below,
        # because that is a commissioning bug rather than a paused household.
        #
        # LAST on purpose — every real graph above has already been considered,
        # so a parked file can never shadow one carrying driver protection.
        parked_text, parked_graph = build_parked_muted_graph(
            topology, config_path=parked_config_path
        )
        if parked_text is not None and parked_graph.allowed:
            # No `event=` line here: this function is a pure decision reached
            # also by read-only callers. `apply_safe_graph_decision_to_statefile`
            # emits the stable `decision=parked_muted` line instead.
            selected = str(parked_muted_config_path(parked_config_path))
            return SafeGraphDecision(
                status=PARKED_MUTED_STATUS,
                selected_config_path=selected,
                reason=PARKED_MUTED_REASON,
                topology_contract=contract,
                current_graph=current_graph,
                preferred_graph=preferred_graph,
                fallback_graph=parked_graph,
                # Proceeding is not the same as being clean: parking is
                # reachable for a topology that still carries blockers, so the
                # decision REPORTS them while `ok` stays True (derived from
                # `status`, never from `issues`). Deliberately `contract.issues`
                # alone — the set the parked verdict declined to refuse on —
                # rather than the wider `issues` list, which also collects "no
                # candidate at this path" noise a parked box is expected to have.
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
    statefile_path: str | Path,
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
        # reached by read-only callers, and a `decision=parked_muted` line from
        # those would read as "the box was just parked" when nothing was written.
        # It DOES fire on a statefile no-op, deliberately: the parked graph has
        # already been RE-PROVED all-muted by this point, and the line means
        # "this apply resolved to parked", not "the statefile changed".
        log_event(
            logger,
            "active_speaker.runtime_graph",
            decision=PARKED_MUTED_STATUS,
            reason=decision.reason,
            topology_mode=decision.topology_contract.classification,
            statefile=str(statefile_path),
            config_path=decision.selected_config_path,
        )
    # The proof stamp goes down only AFTER the pointer it certifies is on disk.
    # Stamping first would leave a failed `write_camilla_statefile` claiming the
    # OLD statefile was proved against the NEW topology — the exact pair the
    # boot gate reads as "no mismatch", which is the one answer that must not
    # come out of a write that did not happen.
    current = read_camilla_statefile_config_path(statefile_path)
    if _path_matches(current, decision.selected_config_path):
        stamp_statefile_topology(statefile_path, topology)
        return False
    write_camilla_statefile(statefile_path, decision.selected_config_path)
    stamp_statefile_topology(statefile_path, topology)
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

    from jasper.dsp_apply import validate_camilla_config  # lazy: test_active_speaker_runtime_contract patches dsp_apply.validate_camilla_config

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
    # CamillaDSP preflight before these bytes become the box's boot graph: an
    # unloadable parked config would crash-loop jasper-camilla, a worse outcome
    # than the blocked deploy this path replaces, so a rejected graph degrades
    # back to blocked. Checked on a per-invocation-unique temp sibling (mkstemp,
    # not a fixed dotfile) so two concurrent writers in this shared dir cannot
    # unlink each other's probe mid-validation. A missing camilladsp binary
    # passes through, the same `ok_to_apply` contract protected staging uses.
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
