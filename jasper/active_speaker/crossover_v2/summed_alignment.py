# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Bind a same-pose summed capture to the static chain that played it."""

from __future__ import annotations

from copy import deepcopy
from functools import partial
import logging
from pathlib import Path
from typing import Any, Callable, Mapping, cast

import numpy as np

from jasper.active_speaker.branch_peak import BranchPeakError, _mixer_mapping, _step_transfer
from jasper.active_speaker.camilla_yaml import driver_baseline_gain_name, driver_delay_name
from jasper.active_speaker.graph_safety import view_from_camilla_dict
from jasper.audio_measurement.household_mic import resolve_setup_calibration
from jasper.audio_measurement.program import ExcitationProgram
from jasper.audio_measurement.program_analysis import analyze_program_capture
from jasper.audio_measurement.program_analysis.model import SummedAlignmentReference
from jasper.audio_measurement.wired_capture import decode_wav_to_mono
from jasper.log_event import log_event

from .contracts import REFERENCE_MARK_DESIGN_AXIS
from .priors import configured_crossover_transfers
from .record_index import measurement_documents, record_path, reopen_measurement_capture


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
            _, mapping = _mixer_mapping(mixer, mixer["channels"]["in"], step["name"])
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
            transfer, _ = _step_transfer(tuple(name for name in names if name != delay_name), filters, freqs_hz)
            configured = configured_response_by_role[role](freqs_hz) * configured_polarity_by_role[role]
            correction = np.divide(transfer, configured, out=np.zeros_like(transfer), where=abs(configured) > 1e-12)
            transfers[role] = cast(Callable[[np.ndarray], np.ndarray], partial(np.interp, xp=freqs_hz, fp=correction))
        return SummedAlignmentReference(freqs_hz, magnitude_db, transfers, band_hz)
    except (KeyError, TypeError, ValueError, BranchPeakError):
        return _unreadable("unsupported_graph")


def _unreadable(reason: str) -> SummedAlignmentReference | None:
    log_event(logging.getLogger(__name__), "program_analysis.summed_reference_unreadable",
              code="summed_reference_unreadable", reason=reason)
    return None


def cached_session_reference(session: Any) -> SummedAlignmentReference | None:
    baseline = session._measure_entry_baseline
    key = baseline.artifact_ref if baseline is not None else None
    cached = getattr(session, "_summed_alignment_reference_cache", None)
    if cached is None or cached[0] != key:
        seam = session._seams.summed_alignment_reference
        cached = session._summed_alignment_reference_cache = (key, seam(baseline, session._preset) if seam else None)
    return cached[1]


def session_reference(bundle_dir: Path, baseline: Any, preset: Any) -> SummedAlignmentReference | None:
    if baseline is None or baseline.reference_mark != REFERENCE_MARK_DESIGN_AXIS:
        return None
    row = next((row for row, doc in measurement_documents(bundle_dir)
                if doc.get("take_id") == baseline.artifact_ref), None)
    if row is None or row.position_deg not in (None, 0) or row.vertical_deg != 0:
        return None
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
    return reference_from_graph(
        summed.freqs_hz, summed.magnitude_db, graph,
        output_channels={output.driver_role: output.index for output in preset.channel_map.outputs},
        configured_response_by_role=configured or {}, configured_polarity_by_role=polarity,
        band_hz=(max(1200.0, summed.validity_floor_hz or 0), 5000.0),
    )
