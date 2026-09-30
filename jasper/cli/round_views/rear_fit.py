# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The rear branches that realize an acoustic rear/front target on one pair take's woofers."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from jasper.active_speaker.crossover_v2.prescription_document import DOCUMENT_KIND
from jasper.active_speaker.crossover_v2.rear_views import PAIR_ROLES, _pair_segments
from jasper.active_speaker.crossover_v2.round_inputs import take_artifact_name
from jasper.active_speaker.rear_calibration import read_rear_calibration
from jasper.active_speaker.rear_fit import (
    ASSUMPTIONS, FIT_BAND_HZ, FIT_POINTS, SUPPRESSION_FLOOR_DB, SUPPRESSION_SWEEP_HZ,
    acoustic_target, build_document, electrical_target, fit, fit_report,
)
from jasper.audio_measurement.evidence_reasons import REASON_SEGMENT_MISSING, EvidenceUnavailable
from jasper.cli._refusal import EXIT_UNREADABLE, EXIT_WRITE_FAILED, read_json_source, stage
from jasper.cli._report import write_report

from ._common import (
    ARTIFACT_BY_VIEW, PROG, _ROUND_DIR_HELP, _ROUND_DIR_METAVAR, _ROUND_TOOL_ERRORS, _write, add_set_argument,
    answer, default_out, resolve_set, round_inputs, subject,
)


def _cmd_rear_fit(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir)
    target = stage(EXIT_UNREADABLE, (ValueError,), lambda: acoustic_target(read_json_source(args.target)))
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, round_dir)
    selected = resolve_set(inputs, args.set).with_records(inputs.session_dir, every_take=args.take is not None)
    take_id = selected.take_id(args.take)
    segments = _pair_segments(next(take for take in selected.takes if take["take_id"] == take_id))
    if segments is None:
        raise EvidenceUnavailable(REASON_SEGMENT_MISSING, {"take_id": take_id})
    freqs, transfers, _ = segments
    measured = ((freqs, transfers[PAIR_ROLES[0]]), (freqs, transfers[PAIR_ROLES[1]]))
    read = subject(inputs, selected, take_ids=[take_id])
    grid = np.geomspace(*FIT_BAND_HZ, FIT_POINTS)
    conditions = {"dataset": Path(args.target).name, "fit_band_hz": [*FIT_BAND_HZ], "fit_tool": f"{PROG} rear-fit",
                  "measured": read, "timing_reference": "front output of this stage"}
    document = read_rear_calibration(build_document(
        fit(grid, electrical_target(target, measured, grid)), conditions=conditions, assumptions=list(ASSUMPTIONS)))
    report = fit_report(document, target, measured)
    spec = ARTIFACT_BY_VIEW[args.command]
    out = args.out or default_out(inputs, round_dir, take_artifact_name(spec.artifact, take_id, selected.role))
    document_path = out.with_name(f"{out.stem}.document.json")
    stage(EXIT_WRITE_FAILED, (OSError,), write_report, {
        "kind": DOCUMENT_KIND, "schema": 1, "base": "saved", "sections": {"rear_calibration": document},
        "rationale": f"Rear branches fitted by {PROG} rear-fit to {Path(args.target).name} on pair take {take_id}.",
    }, None, document_path)
    parameters = {"fit_band_hz": [*FIT_BAND_HZ], "fit_points": FIT_POINTS,
                  "suppression_band_hz": [float(SUPPRESSION_SWEEP_HZ[0]), float(SUPPRESSION_SWEEP_HZ[-1])],
                  "suppression_floor_db": SUPPRESSION_FLOOR_DB, "target": args.target}
    written = _write({"round_dir": str(round_dir), "set_id": selected.set_id, "take_id": take_id,
                      "parameters": parameters, **report, "document": str(document_path)}, None, out, schema=spec.schema)
    suppression = report["suppression"]
    return answer(
        args.command, schema=spec.schema, subject=read, parameters=parameters, out=written,
        document=str(document_path), suppression=suppression,
        line=(f"rear-fit: {take_id}, rear {suppression['max_ratio_db']:+.1f} dB above "
              f"{SUPPRESSION_SWEEP_HZ[0]:g} Hz ({'meets' if suppression['meets_floor'] else 'MISSES'} "
              f"-{SUPPRESSION_FLOOR_DB:g} dB) -> {document_path}"),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "rear-fit", help="the rear branches that realize an acoustic rear/front target on one pair take's woofers",
    )
    parser.add_argument("round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
    add_set_argument(parser, take=True)
    parser.add_argument("--target", required=True,
                        help="an acoustic_targets jts_rear_calibration document (ADR-0318), or - for stdin")
    parser.add_argument("--out", help="artifact destination; the fitted jts_prescription lands beside it")
    parser.set_defaults(func=_cmd_rear_fit)
