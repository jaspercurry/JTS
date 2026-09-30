# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The rear branches that realize an acoustic rear/front target on one pair take's woofers."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from jasper.active_speaker.crossover_v2.prescription_document import saved_document
from jasper.active_speaker.crossover_v2.rear_views import pair_takes
from jasper.active_speaker.crossover_v2.round_inputs import take_artifact_name
from jasper.active_speaker.measurement_programs import PURPOSE_REAR
from jasper.active_speaker.rear_calibration import read_rear_calibration
from jasper.active_speaker.rear_fit import (
    ASSUMPTIONS, FIT_BAND_HZ, FIT_POINTS, SUPPRESSION_FLOOR_DB, SUPPRESSION_SWEEP_HZ,
    acoustic_target, build_document, electrical_target, fit, fit_report, measured_ratio,
)
from jasper.audio_measurement.evidence_reasons import REFUSE_NOT_A_REAR_PAIR, EvidenceUnavailable
from jasper.cli._refusal import EXIT_UNREADABLE, EXIT_WRITE_FAILED, read_json_source, stage
from jasper.cli._report import write_report

from ._common import (
    ARTIFACT_BY_VIEW, PROG, _ROUND_DIR_HELP, _ROUND_DIR_METAVAR, _ROUND_TOOL_ERRORS, _write, add_set_argument,
    answer, default_out, resolve_set_take, round_inputs,
)


def _cmd_rear_fit(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir)
    target = stage(EXIT_UNREADABLE, (ValueError,), lambda: acoustic_target(read_json_source(args.target)))
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, round_dir)
    read, take_id, role, take = resolve_set_take(round_dir, args.set, args.take, None)
    # Only a rear/pair take plays each woofer alone with the rear stage cleared (ADR-0386); a
    # speaker take's rear played through its candidate's own rear stage.
    pairs = pair_takes([take]) if take.get("measurement_purpose") == PURPOSE_REAR else []
    if not pairs:
        raise EvidenceUnavailable(REFUSE_NOT_A_REAR_PAIR, {"take_id": take_id, "purpose": take.get("measurement_purpose")})
    pair, = pairs
    measured = measured_ratio((pair.freqs_hz, pair.front), (pair.freqs_hz, pair.rear))
    grid = np.geomspace(*FIT_BAND_HZ, FIT_POINTS)
    conditions = {"dataset": Path(args.target).name, "fit_band_hz": [*FIT_BAND_HZ], "fit_tool": f"{PROG} rear-fit",
                  "measured": read}
    document = read_rear_calibration(build_document(
        fit(grid, electrical_target(target, measured, grid)), conditions=conditions, assumptions=list(ASSUMPTIONS)))
    report = fit_report(document, target, measured)
    spec = ARTIFACT_BY_VIEW[args.command]
    out = args.out or default_out(inputs, round_dir, take_artifact_name(spec.artifact, take_id, role))
    document_path = out.with_name(f"{out.stem}.document.json")
    stage(EXIT_WRITE_FAILED, (OSError,), write_report, saved_document(
        {"rear_calibration": document},
        f"Rear branches fitted by {PROG} rear-fit to {Path(args.target).name} on pair take {take_id}.",
    ), None, document_path)
    # The document is a muted seed, so its own preview shows no rear; this one unmutes it.
    preview = ["jasper-crossover-prescriber", "judge", "--preview", str(document_path), "--round", str(round_dir),
               "--vary", "rear_calibration.rear_muted=false", "--out-dir", str(out.with_name(f"{out.stem}.preview"))]
    parameters = {"fit_band_hz": [*FIT_BAND_HZ], "fit_points": FIT_POINTS,
                  "suppression_band_hz": [float(SUPPRESSION_SWEEP_HZ[0]), float(SUPPRESSION_SWEEP_HZ[-1])],
                  "suppression_floor_db": SUPPRESSION_FLOOR_DB, "target": args.target}
    written = _write({"round_dir": str(round_dir), "set_id": read.get("set_id"), "take_id": take_id,
                      "parameters": parameters, **report, "document": str(document_path)}, None, out, schema=spec.schema)
    suppression = report["suppression"]
    return answer(
        args.command, schema=spec.schema, subject=read, parameters=parameters, out=written,
        document=str(document_path), preview=preview, suppression=suppression,
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
