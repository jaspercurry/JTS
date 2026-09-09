# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Reconstruct an exact complete-tune capture and predict a replacement candidate.

``--capture-id`` keeps the recorded branch timing and full configuration.
Without it, the legacy independently referenced solo path remains available:

* ``forward-model <round-dir> [--measured-round <round-dir>]`` — what a
  candidate WOULD measure, from this round's banked per-driver solos summed
  through its filters, trims, polarity and residual delay. With
  ``--measured-round`` it is deltaed against that verify-stage round's banked
  VERIFY sum; with no round carrying one, nothing judged it and the record's
  ``acceptance`` says so.
  Computes only: no audio plays and no device is opened, and applying what it
  predicts stays the prescription doors' job. Writes ``forward_model.json``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from jasper.active_speaker.crossover_v2.capture_prediction import capture_prediction
from jasper.active_speaker.crossover_v2.round_captures import RoundCapturesRefused
from jasper.active_speaker.crossover_v2.contracts import DESIGN_AXIS_DEG
from jasper.active_speaker.crossover_v2.forward_model import candidate_from_json
from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL, PHASE_MEASURE
from jasper.active_speaker.crossover_v2.round_views import (
    RoundViewsError,
    forward_model_verify_delta,
)
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    _load_round,
    _view_out,
    _write,
    answer,
    ARTIFACT_BY_VIEW,
    refused_by_name,
    resolved_out,
)

ACCEPTANCE_RUNS = """\
Historical replay examples (the owner's banked captures, not CI tests).
Prediction informs a safe experiment; these are not permission gates. See ADR-0203 and
docs/historical/flat-campaign-2026-08-31.md section 5 for what each postdicts:

  1. jasper-round-views forward-model <r7-measure-round> \\
         --measured-round <r8-verify-round> \\
         --candidate-json <incumbent-filters.json> --residual-delay-us -100

  2. jasper-round-views forward-model <r9-measure-round> \\
         --measured-round <r10-verify-round> --candidate-json <c5-chain.json>
"""


def _cmd_forward_model(args: argparse.Namespace) -> int:
    if args.capture_id is not None:
        if args.residual_delay_us is not None or args.polarity_sign is not None:
            raise RoundViewsError("complete-tune captures derive alignment from configurations; omit legacy overrides")
        try:
            result = capture_prediction(
                Path(args.round_dir), capture_id=args.capture_id, window_ms=args.window_ms,
                candidate_path=Path(args.candidate_json) if args.candidate_json else None,
                basis_candidate_path=Path(args.basis_candidate_json) if args.basis_candidate_json else None,
                candidate_root=Path(args.candidate_root) if args.candidate_root else None,
                measured_round=Path(args.measured_round) if args.measured_round else None,
                measured_capture_id=args.measured_capture_id,
                expected_prediction_fingerprint=args.expected_prediction_fingerprint,
            )
        except RoundCapturesRefused as exc:
            return refused_by_name(exc.reason, exc.detail)
        written = _write(result, args.out, resolved_out(
            Path(args.round_dir), ARTIFACT_BY_VIEW[args.command].artifact,
        ))
        return answer(
            args.command, out=written, **result["summary"],
            line="forward-model: exact-take reconstruction and candidate prediction; plays nothing",
        )
    if any((args.measured_capture_id, args.basis_candidate_json, args.candidate_root, args.window_ms is not None, args.expected_prediction_fingerprint)):
        raise RoundViewsError("exact diagnostic options require --capture-id")
    basis = _load_round(args.round_dir)
    # A candidate file the operator named and this cannot read is the LOAD
    # stage, exactly as the round directory is.
    candidate = stage(
        EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, candidate_from_json,
        args.candidate_json,
        polarity_sign=args.polarity_sign,
        residual_delay_us=args.residual_delay_us,
    )
    result = forward_model_verify_delta(
        basis,
        candidate,
        measured=(
            _load_round(args.measured_round) if args.measured_round else None
        ),
        phase=args.phase,
        position_deg=args.position_deg,
    )
    # No summable pair is no forward model at all; a delta an operator ASKED
    # for and did not get is the same refusal. An unjudged prediction nobody
    # asked to judge is not — that is an answer, and the record carries it.
    if result.prediction is None or (
        args.measured_round is not None and result.delta is None
    ):
        raise RoundViewsError(result.reason)
    predicted = result.prediction
    written = _write(result.to_dict(), args.out, _view_out(args, basis))
    judged = (
        f"judged against {result.measured_round_dir}: max |delta| "
        f"{result.delta['max_abs_db']:.2f} dB, RMS {result.delta['rms_db']:.2f} dB"
        if result.delta is not None else f"NOT JUDGED ({result.reason})"
    )
    return answer(
        args.command, out=written, bins=int(predicted.freqs_hz.size),
        sum_band_hz=list(predicted.sum_band_hz), take_path=predicted.take_path,
        measured_round_dir=result.measured_round_dir,
        max_abs_db=None if result.delta is None else result.delta["max_abs_db"],
        rms_db=None if result.delta is None else result.delta["rms_db"],
        level_offset_db=None if result.delta is None else result.delta["level_offset_db"],
        reason=result.reason,
        line=(
            f"forward-model [predicted, plays nothing]: {predicted.freqs_hz.size} "
            f"bin(s) over {predicted.sum_band_hz[0]:g}-{predicted.sum_band_hz[1]:g} "
            f"Hz from {predicted.take_path}; {judged}"
            f"{f' -> {written}' if written else ''}"
        ),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    forward = sub.add_parser(
        "forward-model",
        help="what a candidate WOULD measure, summed from this round's banked per-driver solos",
        epilog=ACCEPTANCE_RUNS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    forward.add_argument(
        "round_dir", metavar=_ROUND_DIR_METAVAR,
        help=f"{_ROUND_DIR_HELP} whose per-driver solos are the PREDICTION BASIS",
    )
    forward.add_argument(
        "--measured-round", default=None,
        help="round containing the measured take; with --capture-id, also name "
             "--measured-capture-id. Legacy mode uses its banked VERIFY sum",
    )
    forward.add_argument(
        "--candidate-json", default=None,
        help="with --capture-id: a complete saved candidate.json; omit to reconstruct "
             "the recorded tune. Legacy mode accepts "
             "a JSON object with filters_by_role / trim_db_by_role / polarity_sign / "
             "residual_delay_us; omitting it means an uncorrected pair",
    )
    forward.add_argument(
        "--residual-delay-us", type=float, default=None,
        help="RESIDUAL delay in the analysis frame, NOT an applied delay (each banked "
             "solo is referenced to its own direct peak); overrides the candidate file",
    )
    forward.add_argument(
        "--polarity-sign", type=int, default=None, choices=(-1, 1),
        help="the tweeter branch's commanded polarity; overrides the candidate file",
    )
    forward.add_argument(
        "--phase", default=PHASE_MEASURE, choices=(PHASE_MEASURE, PHASE_LATERAL),
        help="which banked phase carries the per-driver solos to sum",
    )
    forward.add_argument(
        "--position-deg", type=int, default=DESIGN_AXIS_DEG,
        help="the bearing whose take is read",
    )
    forward.add_argument("--out", default=None, help="write the result here (- for stdout)")
    forward.add_argument("--capture-id", help="exact complete-tune diagnostic take; without a candidate, check W+T against its own sum")
    forward.add_argument("--measured-capture-id", help="exact changed-candidate diagnostic take to compare; never defaults to another take")
    forward.add_argument("--basis-candidate-json", help="source candidate artifact, checked against the selected take; otherwise resolve its candidate ID from the bank")
    forward.add_argument("--candidate-root", help="candidate bank root for offline source-candidate lookup")
    forward.add_argument("--window-ms", type=float, help="one shared diagnostic window in ms; default is the shipped reference window")
    forward.add_argument("--expected-prediction-fingerprint", help="bind the comparison to the fingerprint in the saved pretrial forecast")
    forward.set_defaults(func=_cmd_forward_model)
