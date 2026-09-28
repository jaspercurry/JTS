# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Runtime safety proofs for roleful active-speaker CamillaDSP graphs."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import (
    Any, Awaitable, Callable, Collection, Literal, Mapping,
)

import yaml

from jasper.audio_measurement.evidence_identity import NormalizedActiveRawIdentity
from jasper.bass_extension.dynamic import validate_dynamic_bass_descriptor
from jasper.bass_extension.dynamic_graph import dynamic_bass_owner_groups, validated_base_graph
from jasper.json_fields import issue as _issue
from jasper.sound.flat_verifier import (
    _flat_graph_allowed,
    _flat_hard_muted_outputs,
    _flat_mono_fold_proved,
    _playback_is_program_bake_pipe,
    _required_mono_fold_output,
)

from jasper.output_topology import OutputTopology
from jasper.output_topology_store import load_output_topology_strict

from .camilla_yaml import _reserialize_keeping_header
from .camilla_names import STARTUP_MUTE_GAIN_DB, output_commission_mute_name as _commission_mute_name
from .graph.active_verifier import _active_graph_evidence
from .graph_types import (
    GRAPH_ALL_MUTED_ACTIVE_STARTUP,
    GRAPH_GUARDED_COMMISSIONING,
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    GRAPH_DRIVER_DOMAIN_BASELINE,
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
)
from .output_contract import (
    ACTIVE_BASELINE_SOURCE,
    ACTIVE_DRIVER_DOMAIN_SOURCE,
    OutputContract,
    classify_output_contract,
    flat_graph_program_dest_map,
    mains_lowest_driver_indexes as _mains_lowest_driver_indexes,
    subwoofer_output_indexes as _subwoofer_output_indexes,
)
from .path_safety import (
    software_guard_ready_for_startup,
    staged_target_signature,
    topology_target_signature,
)

# Explicit evidence for frozen in-memory tests/composition inputs that prove an
# ordinary no-profile baseline. Production persisted hosts obtain the same shape
# only through :func:`classify_bass_extension_graph`.
NO_BASS_EXTENSION_PROFILE_SUMMARY: Mapping[str, Any] = MappingProxyType({
    "authority_valid": True,
    "runtime_block_required": False,
})




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
