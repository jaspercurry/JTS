# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Runtime safety proofs for roleful active-speaker CamillaDSP graphs."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import (
    Any,
    Collection,
    Mapping,
)

import yaml

from jasper.json_fields import issue as _issue
from jasper.sound.flat_verifier import (
    _flat_graph_allowed,
    _flat_hard_muted_outputs,
    _flat_mono_fold_proved,
    _playback_is_program_bake_pipe,
    _required_mono_fold_output,
)

from jasper.audio_routes.output_topology import OutputTopology
from jasper.audio_routes.output_topology_store import load_output_topology_strict

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
)
from .output_contract import (
    OutputContract,
    classify_output_contract,
    flat_graph_program_dest_map,
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
