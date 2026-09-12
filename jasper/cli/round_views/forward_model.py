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
    add_set_argument, answer,
    ARTIFACT_BY_VIEW,
    resolved_out, resolve_set, round_inputs,
)

ACCEPTANCE_RUNS = """jasper-round-views forward-model <basis-round> --set <basis-set>

jasper-round-views forward-model <basis-round> --set <basis-set> \
    --candidate-json <candidate.json> --basis-candidate-json <basis-candidate.json> --measured-round <candidate-round> \
    --measured-set <candidate-set>
"""


def _cmd_forward_model(args: argparse.Namespace) -> int:
    selected = resolve_set(round_inputs(Path(args.round_dir)), args.set)
    measured = resolve_set(round_inputs(Path(args.measured_round)), args.measured_set) if args.measured_round else None
    try:
        exact_result = stage(
            EXIT_UNREADABLE, (OSError,), capture_prediction,
            Path(args.round_dir), capture_id=selected.take_id(args.take), window_ms=args.window_ms,
            candidate_path=Path(args.candidate_json) if args.candidate_json else None,
            basis_candidate_path=Path(args.basis_candidate_json) if args.basis_candidate_json else None,
            candidate_root=Path(args.candidate_root) if args.candidate_root else None,
            measured_round=Path(args.measured_round) if args.measured_round else None,
            measured_capture_id=measured.take_id(args.measured_take) if measured else None,
            expected_prediction_fingerprint=args.expected_prediction_fingerprint,
        )
    except ForwardModelError as exc:
        return failed(EXIT_REFUSED, exc.refusal_reason, {"message": str(exc), **exc.detail})
    except RoundCapturesRefused as exc:
        return failed(EXIT_REFUSED, exc.reason, exc.detail)
    written = _write(exact_result, args.out, resolved_out(
        Path(args.round_dir), ARTIFACT_BY_VIEW[args.command].artifact, args.set,
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
        help="round containing the measured set",
    )
    forward.add_argument(
        "--candidate-json", default=None,
        help="complete saved candidate.json; omit to reconstruct the recorded tune",
    )
    forward.add_argument("--out", default=None, help="write the result here")
    add_set_argument(forward, take=True)
    add_set_argument(forward, name="--measured-set", take=True)
    forward.add_argument("--basis-candidate-json", help="source candidate artifact, checked against the selected take; otherwise resolve its candidate ID from the bank")
    forward.add_argument("--candidate-root", help="candidate bank root for offline source-candidate lookup")
    forward.add_argument("--window-ms", type=float, help="one shared diagnostic window in ms; default is the shipped reference window")
    forward.add_argument("--expected-prediction-fingerprint", help="bind the comparison to the fingerprint in the saved pretrial forecast")
    forward.set_defaults(func=_cmd_forward_model)
