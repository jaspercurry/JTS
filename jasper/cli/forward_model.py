# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator door onto the forward model: what a candidate WOULD measure.

Two verbs, both offline. **No audio plays and no device is opened** — an
existing MEASURE bank answers both today.

``predict``
    Read a bundle's banked per-driver solos, sum them through one candidate's
    filters, trims, delay and polarity, and print the predicted summed
    magnitude on the bank's own grid.

``verify-delta``
    The same prediction over a BANKED ROUND, deltaed against a banked VERIFY
    sum (ticket 4.5). The two halves come from two rounds, because the flow
    banks them in two — a measure stage walks the solos, a verify stage
    measures the sum, in separate bundles — so ``--measured-round`` names the
    second and the output discloses both. Additive evidence: the delta is
    facts — band, points, level offset, per-bin dB, max and RMS — and no
    verdict.

**A refusal is an output, not an error.** A bundle with no take carrying both
solos cannot support a prediction, and the sentence saying so is printed from
the module that decided it. Nothing here ranks candidates or scores them: a
prediction is an instrument, and what a given delta MEANS is the reader's
judgement (invariant 3).

Applying anything this tool predicts is NOT its job. The prescription doors own
that, with their own gates and their own receipts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from jasper.active_speaker.crossover_v2.contracts import (
    DESIGN_AXIS_DEG,
    DRIVER_ROLE_TWEETER,
    DRIVER_ROLE_WOOFER,
)
from jasper.active_speaker.crossover_v2.forward_model import (
    ACCEPTANCE_NOT_RUN,
    REFUSAL_NO_CURVE_PAIR,
    ForwardModelError,
    SummationCandidate,
    load_branch_pair,
    predict_sum,
)
from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL, PHASE_MEASURE
from jasper.active_speaker.crossover_v2.round_views import (
    RoundViewsError,
    forward_model_verify_delta,
    load_banked_round,
)

from ._logging import configure_verbose_logging
from ._refusal import EXIT_OK, EXIT_REFUSED, EXIT_UNREADABLE, failed

#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204).
AUTHORITY_TIER = "advisory (plays nothing)"

REFUSE_CANDIDATE = "forward_model_unreadable_candidate"
REFUSE_NO_DELTA = "forward_model_no_verify_delta"

ACCEPTANCE_RUNS = f"""\
operator acceptance (run against the owner's banked captures, NOT CI tests):

  1. Postdict the flat campaign's r8 regression. r8 applied the measured
     -100 us inter-driver delay under EQ held verbatim from the incumbent
     tune, and the 5-seat verify came back a REGRESSION: -3.1 dB at the
     crossover region, auto-restored. The two halves sit in two banked
     rounds, because the flow banks them that way -- a measure-stage round
     walks the per-driver solos and a verify-stage round measures the sum:

       jasper-forward-model verify-delta <r7-measure-round> \\
           --measured-round <r8-verify-round> \\
           --candidate-json <incumbent-filters.json> \\
           --residual-delay-us -100

     The model passes if the predicted delta reproduces that
     crossover-region dip (~-3 dB) rather than predicting an improvement.
     See historical/flat-campaign-2026-08-31.md section 5.

  2. Track the C5 -> final measured delta. C5 is the blind-run 22-filter
     starting chain; the final tune is 24 filters. Predict each chain over
     the same banked solos and compare the predicted C5 -> final change
     against the measured one (grade 1.112 -> 0.9035 over r9/r10, then
     0.93 with tilt-removed RMS 0.18 -> 0.067 over r11/r12). The model
     passes if it tracks that measured delta within its stated tolerance.

Both are campaign-entry acceptance for ADR-0203's recommissioning campaign,
and both must pass BEFORE any prediction is used to triage a candidate.

Every record this tool prints carries its own `acceptance` block, so the
question is answerable from the output rather than from this help: `predict`
compares its curve to nothing and says `{ACCEPTANCE_NOT_RUN}`, while
`verify-delta` names the banked round that judged it.
"""


def _candidate(args: argparse.Namespace) -> SummationCandidate:
    """One candidate from its JSON source plus the two single-value flags.

    The flags OVERRIDE the file when given, so a held-EQ postdiction varies
    only the delay: the same candidate file, one flag moved.
    """

    raw: Mapping[str, Any] = {}
    if args.candidate_json is not None:
        loaded = json.loads(Path(args.candidate_json).read_text())
        if not isinstance(loaded, Mapping):
            raise ValueError("candidate JSON must be an object")
        raw = loaded
    filters = raw.get("filters_by_role") or {}
    trims = raw.get("trim_db_by_role") or {}
    if not isinstance(filters, Mapping) or not isinstance(trims, Mapping):
        raise ValueError("filters_by_role and trim_db_by_role must be objects")
    polarity = raw.get("polarity_sign", 1)
    delay = raw.get("residual_delay_us", 0.0)
    if args.polarity_sign is not None:
        polarity = args.polarity_sign
    if args.residual_delay_us is not None:
        delay = args.residual_delay_us
    return SummationCandidate(
        filters_by_role={
            str(role): list(entries) for role, entries in filters.items()
        },
        trim_db_by_role={str(role): float(db) for role, db in trims.items()},
        polarity_sign=int(polarity),
        residual_delay_us=float(delay),
    )


def _cmd_predict(args: argparse.Namespace) -> int:
    try:
        candidate = _candidate(args)
    except (OSError, ValueError) as exc:
        return failed(
            EXIT_REFUSED, REFUSE_CANDIDATE, f"{args.candidate_json}: {exc}"
        )

    pair = load_branch_pair(
        Path(args.bundle_dir),
        phase=args.phase,
        position_deg=args.position_deg,
        woofer_role=args.woofer_role,
        tweeter_role=args.tweeter_role,
    )
    if pair is None:
        return failed(
            EXIT_REFUSED,
            REFUSAL_NO_CURVE_PAIR,
            f"{args.bundle_dir}: no {args.phase} take at {args.position_deg} deg "
            f"carries curves for both {args.woofer_role!r} and "
            f"{args.tweeter_role!r}",
        )
    predicted = predict_sum(pair, candidate)
    print(json.dumps(
        {"status": "predicted", "prediction": predicted.to_dict()},
        indent=2, sort_keys=True,
    ))
    # The gate lived only in this tool's --help and never crossed a driver's
    # path (#3481): output that does not say it is untriaged reads as
    # authoritative as output a measurement checked.
    print(
        f"acceptance {ACCEPTANCE_NOT_RUN}: no measurement judged this "
        "prediction — see ACCEPTANCE_RUNS in --help for the two runs that "
        "would, and `verify-delta` for the verb that records one",
        file=sys.stderr,
    )
    print(
        f"predicted {predicted.freqs_hz.size} bins over "
        f"{predicted.sum_band_hz[0]:g}-{predicted.sum_band_hz[1]:g} Hz "
        f"from {predicted.take_path}",
        file=sys.stderr,
    )
    return EXIT_OK


def _cmd_verify_delta(args: argparse.Namespace) -> int:
    try:
        candidate = _candidate(args)
    except (OSError, ValueError) as exc:
        return failed(
            EXIT_REFUSED, REFUSE_CANDIDATE, f"{args.candidate_json}: {exc}"
        )
    measured_dir = args.measured_round or args.round_dir
    try:
        banked = load_banked_round(Path(args.round_dir))
        measured = (
            banked if measured_dir == args.round_dir
            else load_banked_round(Path(measured_dir))
        )
    except RoundViewsError as exc:
        return failed(EXIT_REFUSED, REFUSE_NO_DELTA, str(exc))

    result = forward_model_verify_delta(
        banked, candidate, measured=measured,
        phase=args.phase, position_deg=args.position_deg,
    )
    if result.delta is None:
        return failed(EXIT_REFUSED, REFUSE_NO_DELTA, result.reason)
    print(json.dumps(
        {"status": "compared",
         "acceptance": result.acceptance,
         "basis_round_dir": result.basis_round_dir,
         "measured_round_dir": result.measured_round_dir,
         "predicted_minus_measured": dict(result.delta)},
        indent=2, sort_keys=True,
    ))
    print(
        f"predicted-vs-measured over "
        f"{result.delta['compared_band_hz'][0]:g}-"
        f"{result.delta['compared_band_hz'][1]:g} Hz: "
        f"max |delta| {result.delta['max_abs_db']:.2f} dB, "
        f"RMS {result.delta['rms_db']:.2f} dB "
        f"(solos {result.basis_round_dir}, verify {result.measured_round_dir})",
        file=sys.stderr,
    )
    return EXIT_OK


def _add_common(child: argparse.ArgumentParser) -> None:
    child.add_argument(
        "--candidate-json",
        help="a JSON object with filters_by_role / trim_db_by_role / "
             "polarity_sign / residual_delay_us; omitted means an "
             "uncorrected, untrimmed, in-phase pair at zero residual delay",
    )
    child.add_argument(
        "--residual-delay-us", type=float, default=None,
        help="RESIDUAL delay in the analysis frame, NOT an applied delay — "
             "each banked solo is referenced to its own direct peak, so the "
             "physical arrival gap is already out of the pair. Overrides the "
             "candidate file",
    )
    child.add_argument(
        "--polarity-sign", type=int, default=None, choices=(-1, 1),
        help="the tweeter branch's commanded polarity; overrides the "
             "candidate file",
    )
    child.add_argument(
        "--phase", default=PHASE_MEASURE, choices=(PHASE_MEASURE, PHASE_LATERAL),
        help="which banked phase carries the per-driver solos to sum",
    )
    child.add_argument(
        "--position-deg", type=int, default=DESIGN_AXIS_DEG,
        help="the bearing whose take is read",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-forward-model",
        description=(
            "Predict a candidate's summed response from banked per-driver "
            "solos. Computes only; plays nothing and opens no device."
        ),
        epilog=ACCEPTANCE_RUNS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    predict = sub.add_parser(
        "predict", help="the predicted summed magnitude, from a bundle's bank"
    )
    predict.add_argument(
        "bundle_dir",
        help="a commissioning bundle directory (the one holding info.json "
             "beside evidence/v1/artifacts/crossover_v2/<relay-session-id>/)",
    )
    predict.add_argument("--woofer-role", default=DRIVER_ROLE_WOOFER)
    predict.add_argument("--tweeter-role", default=DRIVER_ROLE_TWEETER)
    _add_common(predict)
    predict.set_defaults(func=_cmd_predict)

    delta = sub.add_parser(
        "verify-delta",
        help="the predicted sum deltaed against a banked round's measured "
             "VERIFY sum",
    )
    delta.add_argument(
        "round_dir",
        help="the banked round whose per-driver solos are the PREDICTION "
             "BASIS (a bank-crossover-round.sh output holding bundle/ and "
             "state.json) — a measure-stage round",
    )
    delta.add_argument(
        "--measured-round",
        default=None,
        help="the banked round whose VERIFY sum is the MEASURED half — a "
             "verify-stage round. Defaults to round_dir, which only answers "
             "for a corpus whose rounds carry both halves; the two-stage flow "
             "banks the solos and the verify in separate rounds",
    )
    _add_common(delta)
    delta.set_defaults(func=_cmd_verify_delta)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_verbose_logging(verbose=args.verbose)
    try:
        return int(args.func(args))
    except ForwardModelError as exc:
        # Verbatim: a bank that cannot support a prediction is a finding about
        # the bank, and the module that decided it owns the sentence.
        return failed(EXIT_REFUSED, exc.refusal_reason, str(exc))
    except OSError as exc:
        print(f"unreadable bundle: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
