# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What a candidate would measure, summed from the banked per-driver solos.

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
import sys

from jasper.active_speaker.crossover_v2.contracts import DESIGN_AXIS_DEG
from jasper.active_speaker.crossover_v2.forward_model import candidate_from_json
from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL, PHASE_MEASURE
from jasper.active_speaker.crossover_v2.round_views import (
    RoundViewsError,
    forward_model_verify_delta,
)
from jasper.cli._refusal import EXIT_OK, EXIT_UNREADABLE, stage

from ._common import (
    _ROUND_DIR_HELP,
    _ROUND_TOOL_ERRORS,
    _load_round,
    _view_out,
    _write,
)

#: The two runs that must pass before a prediction triages a candidate, as
#: invocations rather than prose. The pointers below own what each postdicts.
ACCEPTANCE_RUNS = """\
operator acceptance (the owner's banked captures, NOT CI tests) -- both must
pass before a prediction triages a candidate. See ADR-0203 and
docs/historical/flat-campaign-2026-08-31.md section 5 for what each postdicts:

  1. jasper-round-views forward-model <r7-measure-round> \\
         --measured-round <r8-verify-round> \\
         --candidate-json <incumbent-filters.json> --residual-delay-us -100

  2. jasper-round-views forward-model <r9-measure-round> \\
         --measured-round <r10-verify-round> --candidate-json <c5-chain.json>
"""


def _cmd_forward_model(args: argparse.Namespace) -> int:
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
    print(
        f"forward-model [predicted, plays nothing]: {predicted.freqs_hz.size} "
        f"bin(s) over {predicted.sum_band_hz[0]:g}-{predicted.sum_band_hz[1]:g} "
        f"Hz from {predicted.take_path}; {judged}"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


def add_parser(sub: argparse._SubParsersAction) -> None:
    forward = sub.add_parser(
        "forward-model",
        help="what a candidate WOULD measure, summed from this round's banked per-driver solos",
        epilog=ACCEPTANCE_RUNS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    forward.add_argument(
        "round_dir",
        help=f"{_ROUND_DIR_HELP} whose per-driver solos are the PREDICTION BASIS",
    )
    forward.add_argument(
        "--measured-round", default=None,
        help="a verify-stage round whose banked VERIFY sum judges the prediction. "
             "Defaults to round_dir, which judges only for a round carrying both "
             "halves; the two-stage flow banks the solos and the verify apart",
    )
    forward.add_argument(
        "--candidate-json", default=None,
        help="a JSON object with filters_by_role / trim_db_by_role / polarity_sign / "
             "residual_delay_us; omitted means an uncorrected, untrimmed, in-phase "
             "pair at zero residual delay",
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
    forward.set_defaults(func=_cmd_forward_model)
