# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Reconstruct exact complete-tune takes or forecast replacement candidates."""

from __future__ import annotations

import argparse
from pathlib import Path

from jasper.active_speaker.crossover_v2.capture_prediction import capture_prediction
from jasper.active_speaker.crossover_v2.round_captures import RoundCapturesRefused
from jasper.active_speaker.crossover_v2.forward_model import ForwardModelError
from jasper.cli._refusal import EXIT_REFUSED, EXIT_UNREADABLE, failed, stage

from ._common import (
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _write,
    answer,
    ARTIFACT_BY_VIEW,
    resolved_out,
)

ACCEPTANCE_RUNS = """\
Replay an exact diagnostic capture, then compare a candidate forecast with its take:

  jasper-round-views forward-model <basis-round> --capture-id <basis-take>

  jasper-round-views forward-model <basis-round> --capture-id <basis-take> \\
      --candidate-json <candidate.json> --basis-candidate-json <basis-candidate.json> \\
      --measured-round <candidate-round> \\
      --measured-capture-id <candidate-take>
"""


def _cmd_forward_model(args: argparse.Namespace) -> int:
    try:
        exact_result = stage(
            EXIT_UNREADABLE, (OSError,), capture_prediction,
            Path(args.round_dir), capture_id=args.capture_id, window_ms=args.window_ms,
            candidate_path=Path(args.candidate_json) if args.candidate_json else None,
            basis_candidate_path=Path(args.basis_candidate_json) if args.basis_candidate_json else None,
            candidate_root=Path(args.candidate_root) if args.candidate_root else None,
            measured_round=Path(args.measured_round) if args.measured_round else None,
            measured_capture_id=args.measured_capture_id,
            expected_prediction_fingerprint=args.expected_prediction_fingerprint,
        )
    except ForwardModelError as exc:
        return failed(EXIT_REFUSED, exc.refusal_reason, {"message": str(exc), **exc.detail})
    except RoundCapturesRefused as exc:
        return failed(EXIT_REFUSED, exc.reason, exc.detail)
    written = _write(exact_result, args.out, resolved_out(
        Path(args.round_dir), ARTIFACT_BY_VIEW[args.command].artifact,
    ))
    return answer(
        args.command, out=written, **exact_result["summary"],
        line="forward-model: exact-take reconstruction and candidate prediction; plays nothing",
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    forward = sub.add_parser(
        "forward-model",
        help="reconstruct an exact complete-tune take or forecast a candidate change",
        epilog=ACCEPTANCE_RUNS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    forward.add_argument(
        "round_dir", metavar=_ROUND_DIR_METAVAR,
        help=f"{_ROUND_DIR_HELP} containing the measured branch basis",
    )
    forward.add_argument(
        "--measured-round", default=None,
        help="round containing --measured-capture-id",
    )
    forward.add_argument(
        "--candidate-json", default=None,
        help="complete saved candidate.json; omit to reconstruct the recorded tune",
    )
    forward.add_argument("--out", default=None, help="write the result here (- for stdout)")
    forward.add_argument("--capture-id", required=True, help="exact complete-tune diagnostic take; without a candidate, check W+T against its own sum")
    forward.add_argument("--measured-capture-id", help="exact changed-candidate diagnostic take to compare; never defaults to another take")
    forward.add_argument("--basis-candidate-json", help="source candidate artifact, checked against the selected take; otherwise resolve its candidate ID from the bank")
    forward.add_argument("--candidate-root", help="candidate bank root for offline source-candidate lookup")
    forward.add_argument("--window-ms", type=float, help="one shared diagnostic window in ms; default is the shipped reference window")
    forward.add_argument("--expected-prediction-fingerprint", help="bind the comparison to the fingerprint in the saved pretrial forecast")
    forward.set_defaults(func=_cmd_forward_model)
