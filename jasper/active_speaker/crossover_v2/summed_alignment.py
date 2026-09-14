# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Bind a same-pose summed capture to the static chain that played it."""

from __future__ import annotations

from copy import deepcopy
from functools import partial
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from jasper.active_speaker.branch_peak import _step_transfer
from jasper.active_speaker.camilla_yaml import driver_baseline_gain_name, driver_delay_name
from jasper.active_speaker.graph_safety import view_from_camilla_dict
from jasper.audio_measurement.household_mic import resolve_setup_calibration
from jasper.audio_measurement.program import ExcitationProgram
from jasper.audio_measurement.program_analysis import analyze_program_capture
from jasper.audio_measurement.program_analysis.model import SummedAlignmentReference
from jasper.audio_measurement.wired_capture import decode_wav_to_mono

from .contracts import REFERENCE_MARK_DESIGN_AXIS
from .priors import configured_crossover_transfers
from .record_index import measurement_documents, record_path, reopen_measurement_capture


def reference_from_graph(
    freqs_hz: np.ndarray, magnitude_db: np.ndarray, graph: Mapping[str, Any], *,
    output_channels: Mapping[str, int], configured_response_by_role: Mapping[str, Any],
    configured_polarity_by_role: Mapping[str, int], band_hz: tuple[float, float],
) -> SummedAlignmentReference:
    filters = deepcopy(graph["filters"])
    view = view_from_camilla_dict(graph)
    transfers = {}
    for role, channel in output_channels.items():
        gain = filters.get(driver_baseline_gain_name(role))
        if gain is not None:
            gain["parameters"]["inverted"] = configured_polarity_by_role[role] < 0
        names = tuple(
            name for step in view.pipeline_steps if channel in step.channels
            for name in step.names
            if name != driver_delay_name(role) and filters[name]["type"] != "Limiter"
        )
        transfers[role] = partial(
            _correction_transfer, names=names, filters=filters,
            crossover=configured_response_by_role[role],
            polarity=configured_polarity_by_role[role],
        )
    return SummedAlignmentReference(freqs_hz, magnitude_db, transfers, band_hz)


def _correction_transfer(freqs, *, names, filters, crossover, polarity):
    transfer, _ = _step_transfer(names, filters, freqs)
    configured = crossover(freqs) * polarity
    return np.divide(transfer, configured, out=np.zeros_like(transfer), where=abs(configured) > 1e-12)


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
        return None
    calibration = resolve_setup_calibration(record.get("capture_setup"), device=record.get("capture_device"))
    program = ExcitationProgram.from_dict(record["program"])
    samples, rate = decode_wav_to_mono(wav)
    summed = analyze_program_capture(
        program, samples, rate, calibration=calibration.curve if calibration else None,
    ).summed_response
    if summed is None:
        return None
    configured, polarity = configured_crossover_transfers(preset)
    # The lab comparator excludes the nonlinear LF extension; its static branch
    # and common filters remain. Coherent stereo split gain cancels in tracking RMS.
    return reference_from_graph(
        summed.freqs_hz, summed.magnitude_db, graph,
        output_channels={output.driver_role: output.index for output in preset.channel_map.outputs},
        configured_response_by_role=configured or {}, configured_polarity_by_role=polarity,
        band_hz=(max(1200.0, summed.validity_floor_hz or 0), 5000.0),
    )
