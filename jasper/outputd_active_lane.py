# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Admission proof for outputd's active content lane."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jasper import paths
from jasper.active_speaker.environment import parse_camilla_statefile_config_path
from jasper.active_speaker.graph_types import (
    GRAPH_ALL_MUTED_ACTIVE_STARTUP,
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    GRAPH_DRIVER_DOMAIN_BASELINE,
    GRAPH_GUARDED_COMMISSIONING,
    GRAPH_PROGRAM_BAKE_PIPE,
    GraphSafety,
)
from jasper.active_speaker.runtime_contract import classify_bass_extension_graph
from jasper.active_speaker.state_paths import baseline_profile_state_path
from jasper.output_topology import OutputTopology
from jasper.output_topology_store import load_output_topology_strict

# Every playback device a legal outputd ENDPOINT graph may name. ONE member: the
# ACTIVE RING is the only transport carrying POST-crossover per-driver channels
# to outputd. A frozenset rather than a single `==` because membership is the
# seam the endpoint width probe reads — it must reject everything outside the
# set, notably the STEREO ring and the retired snd-aloop lane (ADR-0100).
#
# Redeclared rather than imported from jasper.fanin_coupling: this module is the
# runtime VERIFIER's independent copy of the endpoint vocabulary, and a contract
# test pins the copies equal.
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

    from jasper.active_speaker.staging import staged_metadata_path as default_staged_path  # lazy: test_ring_active_endpoint patches staging.staged_metadata_path

    primary_statefile = paths.camilla_statefile(statefile_path)
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

    crossover_statefile = paths.crossover_statefile(crossover_statefile_path)
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
