# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Bind a same-pose summed capture to the static chain that played it."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from functools import partial
import logging
from pathlib import Path
from typing import Any, Callable, Mapping, cast

import numpy as np

from jasper.active_speaker.graph_transfer import GraphTransferError, filter_transfer, mixer_mapping
from jasper.active_speaker.camilla_names import driver_baseline_gain_name, driver_delay_name
from jasper.active_speaker.graph_safety import view_from_camilla_dict
from jasper.audio_measurement.household_mic import resolve_setup_calibration
from jasper.audio_measurement.program import ExcitationProgram
from jasper.audio_measurement.program_analysis import analyze_program_capture
from jasper.audio_measurement.program_analysis.model import SummedAlignmentReference
from jasper.audio_measurement.wired_capture import decode_wav_to_mono
from jasper.log_event import log_event

from .contracts import REFERENCE_MARK_DESIGN_AXIS
from .measurement_context import capture_basis
from .priors import configured_crossover_transfers
from .record_index import Measurement, measurement_documents, played_graph_fingerprint, record_path, reopen_measurement_capture
from .round_evidence import EntryBaseline, measured_response_from_analysis


def banked_entry_baseline(record: Mapping[str, Any], analysis: Any) -> EntryBaseline | None:
    if record.get("graph_scope") != "timing":
        _unreadable("entry_baseline_scope")
        return None
    if record.get("position_deg") != 0 or record.get("vertical_deg", 0) != 0:
        return None
    measured = measured_response_from_analysis(analysis, reference_mark=REFERENCE_MARK_DESIGN_AXIS)
    return EntryBaseline.from_measurement(
        measured, graph_fingerprint=played_graph_fingerprint(record),
        captured_at=str(record.get("captured_at") or "unknown"), artifact_ref=record["take_id"],
    ) if measured is not None else None


def reference_from_graph(
    freqs_hz: np.ndarray, magnitude_db: np.ndarray, graph: Mapping[str, Any], *,
    output_channels: Mapping[str, int], configured_response_by_role: Mapping[str, Any],
    configured_polarity_by_role: Mapping[str, int], band_hz: tuple[float, float],
) -> SummedAlignmentReference | None:
    try:
        filters = deepcopy(graph["filters"])
        view = view_from_camilla_dict(graph)
        for step in graph.get("pipeline", ()):
            if step["type"] != "Mixer":
                continue
            mixer = graph["mixers"][step["name"]]
            _, mapping = mixer_mapping(mixer, mixer["channels"]["in"], step["name"])
            if any(gain.real < 0 for dest, sources in mapping
                   if dest in output_channels.values() for _, gain in sources):
                return _unreadable("mixer_polarity")
        transfers = {}
        for role, channel in output_channels.items():
            names = tuple(name for step in view.pipeline_steps if channel in step.channels for name in step.names)
            gain_name, delay_name = driver_baseline_gain_name(role), driver_delay_name(role)
            if any(name not in names or filters.get(name, {}).get("type") != kind
                   for name, kind in ((gain_name, "Gain"), (delay_name, "Delay"))):
                return _unreadable("missing_alignment_filter")
            if role not in configured_response_by_role or role not in configured_polarity_by_role:
                return _unreadable("missing_crossover_region")
            filters[gain_name]["parameters"]["inverted"] = configured_polarity_by_role[role] < 0
            transfer = filter_transfer(tuple(name for name in names if name != delay_name), filters, freqs_hz)
            configured = configured_response_by_role[role](freqs_hz) * configured_polarity_by_role[role]
            correction = np.divide(transfer, configured, out=np.zeros_like(transfer), where=abs(configured) > 1e-12)
            transfers[role] = cast(Callable[[np.ndarray], np.ndarray], partial(np.interp, xp=freqs_hz, fp=correction))
        return SummedAlignmentReference(freqs_hz, magnitude_db, transfers, band_hz)
    except (KeyError, TypeError, ValueError, GraphTransferError):
        return _unreadable("unsupported_graph")


def _unreadable(reason: str) -> SummedAlignmentReference | None:
    log_event(logging.getLogger(__name__), "active_speaker.summed_reference_unreadable",
              code="summed_reference_unreadable", reason=reason)
    return None


def cached_session_reference(session: Any) -> SummedAlignmentReference | None:
    baseline = session._measure_entry_baseline
    key = baseline.artifact_ref if baseline is not None else None
    cached = getattr(session, "_summed_alignment_reference_cache", None)
    if cached is None or cached[0] != key:
        seam = session._seams.summed_alignment_reference
        reference = (_unreadable("no_entry_baseline") if baseline is None else
                     seam(baseline, session._preset) if seam else None)
        cached = session._summed_alignment_reference_cache = (key, reference)
    return cached[1]


def session_reference(bundle_dir: Path, baseline: Any, preset: Any) -> SummedAlignmentReference | None:
    if baseline is None or baseline.reference_mark != REFERENCE_MARK_DESIGN_AXIS:
        return None
    documents = list(measurement_documents(bundle_dir))
    anchor = next(((row, doc) for row, doc in documents if doc.get("take_id") == baseline.artifact_ref), None)
    if anchor is None or (anchor[0].position_deg, anchor[0].vertical_deg, anchor[0].graph_scope) != (0, 0, "timing"):
        return None
    anchor_row, anchor_doc = anchor
    selected: dict[str, Measurement] = {}
    for row, doc in documents:
        if (row.position_deg != 0 or row.vertical_deg != 0 or row.graph_scope != "timing"
                or (row.session_id, row.phase) != (anchor_row.session_id, anchor_row.phase)
                or capture_basis(doc) != capture_basis(anchor_doc)):
            continue
        selected[doc["take_id"]] = row
    references = tuple(reference for row in selected.values()
                       if (reference := _capture_reference(bundle_dir, row, preset)) is not None)
    return replace(references[0], repeat_responses=references[1:]) if references else None


def _capture_reference(bundle_dir: Path, row: Measurement, preset: Any) -> SummedAlignmentReference | None:
    record, wav = reopen_measurement_capture(bundle_dir, record_path(row))
    graph = (record.get("provenance") or {}).get("graph", {}).get("config")
    if graph is None or wav is None:
        return _unreadable("missing_capture_graph")
    calibration = resolve_setup_calibration(record.get("capture_setup"), device=record.get("capture_device"))
    program = ExcitationProgram.from_dict(record["program"])
    samples, rate = decode_wav_to_mono(wav)
    summed = analyze_program_capture(
        program, samples, rate, calibration=calibration.curve if calibration else None,
    ).summed_response
    if summed is None:
        return None
    configured, polarity = configured_crossover_transfers(preset)
    reference = reference_from_graph(
        summed.freqs_hz, summed.magnitude_db, graph,
        output_channels={output.driver_role: output.index for output in preset.channel_map.outputs},
        configured_response_by_role=configured or {}, configured_polarity_by_role=polarity,
        band_hz=(max(1200.0, summed.validity_floor_hz or 0), 5000.0),
    )
    return replace(reference, graph_fingerprint=played_graph_fingerprint(record)) if reference else None
