# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Prove bass-extension graph snapshots against their saved authority."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Collection, Literal, Mapping

import yaml

from jasper.audio_measurement.evidence_identity import NormalizedActiveRawIdentity
from jasper.bass_extension.dynamic import validate_dynamic_bass_descriptor
from jasper.bass_extension.dynamic_graph import dynamic_bass_owner_groups, validated_base_graph
from jasper.audio_routes.output_topology import OutputTopology

from ..camilla_yaml import _reserialize_keeping_header
from ..environment import classify_camilla_config_text, parse_camilla_statefile_config_path
from ..graph_types import GRAPH_APPROVED_ACTIVE_RUNTIME, GraphSafety
from ..output_contract import (
    ACTIVE_BASELINE_SOURCE,
    ACTIVE_DRIVER_DOMAIN_SOURCE,
    bass_extension_output_indexes,
    classify_output_contract,
)
from ..runtime_contract import (
    NO_BASS_EXTENSION_PROFILE_SUMMARY,
    _unsafe_boundary,
    classify_camilla_graph,
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
            channels = tuple(sorted(bass_extension_output_indexes(contract)))
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
