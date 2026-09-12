# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Start an inline plan, place the microphone, read progress and bank a run."""
from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Any, Sequence

from jasper.active_speaker.wizard_client import (
    CSRF_PAGE_PATH, STATUS_PATH, REASON_ANSWER_LOST, REASON_NO_FINGERPRINT,
    WizardClient, apply_by_fingerprint, error_of, wait_for_round,
)
from jasper.identity.reader import CROSSOVER_PAGE_PATH, read_identity, speaker_url

from ._refusal import (
    EXIT_OK as EXIT_OK,
    EXIT_REFUSED, EXIT_UNREADABLE, EXIT_WRITE_FAILED, answered, failed,
)

PROG = "jasper-round"
REPUBLISH_PATH = "/sound/speaker/crossover/v2/republish"
DEFAULT_TIMEOUT_S = 900.0
DEFAULT_POLL_S = 5.0
AUTHORITY_TIER = "mutating-with-gates (`run`/`placed`/`wait`/`apply` write; `status` reads)"
LOST_ANSWER_ADVICE = "the apply may have taken effect; read the live candidate before trying again"


def _answer(verb: str, human: str, **fields: Any) -> int:
    return answered({"verb": verb, **fields}, human)


def _round_session_dir(capture_id: str) -> str:
    from jasper.active_speaker.bundles import sessions_dir  # lazy: wait-only measurement imports
    from jasper.active_speaker.crossover_v2.round_inputs import round_artifact_dir  # lazy: wait-only

    found = ""
    try:
        for bundle in sessions_dir().iterdir():
            if not (bundle / "info.json").is_file():
                continue
            round_dir, _ = round_artifact_dir(bundle)
            if round_dir is not None and round_dir.name == capture_id:
                if found:
                    return ""
                found = str(bundle)
    except OSError:
        return ""
    return found


def _wizard_failure(exit_code: int, reason: str, detail: dict, payload: Any) -> int:
    error = error_of(payload)
    fields = error if isinstance(error, dict) else {}
    return failed(
        exit_code, str(fields.get("code") or reason), {**detail, "error": error},
        code=fields.get("code"), next_action=fields.get("next_action"),
    )


def _cmd_run(client: WizardClient, args: argparse.Namespace) -> int:
    from jasper.active_speaker.crossover_v2.contracts import CrossoverV2FlowError  # lazy: run-only
    from ._run_request import resolve_run  # lazy: run-only measurement imports

    try:
        report = resolve_run(args)
    except (ValueError, OSError, CrossoverV2FlowError) as exc:
        return failed(EXIT_REFUSED, getattr(exc, "reason", "program_plan_shape_invalid"), str(exc))
    if args.dry_run:
        answered({"verb": "run", "dry_run": args.dry_run, **report.to_dict()})
        return EXIT_REFUSED if report.blocking else EXIT_OK
    if report.blocking:
        return failed(EXIT_REFUSED, report.issues[0].code, report.to_dict())
    http, payload = client.open_session(report.plan.to_dict())
    if http != 200:
        return _wizard_failure(EXIT_UNREADABLE if http == 0 else EXIT_REFUSED,
                               "run_refused", {"http": http}, payload)
    capture = payload.get("capture", payload) if isinstance(payload, dict) else None
    run_id = capture.get("session_id") if isinstance(capture, dict) else None
    if not isinstance(capture, dict) or not isinstance(run_id, str) or not run_id:
        return failed(EXIT_UNREADABLE, "run_answer_invalid", payload)
    return _answer("run", "Run ready; place the microphone to start.",
                   run_id=run_id, link=speaker_url(CROSSOVER_PAGE_PATH),
                   status_url=speaker_url(STATUS_PATH),
                   shape="trial" if report.plan.candidates else "measure",
                   first_prompt=capture.get("first_prompt"), schedule=report.to_dict())


def _cmd_placed(client: WizardClient, args: argparse.Namespace) -> int:
    http, payload = client.placed(args.run, args.pose)
    if http != 200:
        return _wizard_failure(EXIT_UNREADABLE if http == 0 else EXIT_REFUSED,
                               "placement_refused", {"run_id": args.run, "http": http}, payload)
    return answered(payload)


def _cmd_status(client: WizardClient, args: argparse.Namespace) -> int:
    http, payload = client.run_status(args.run)
    if http != 200:
        return _wizard_failure(EXIT_UNREADABLE if http == 0 else EXIT_REFUSED,
                               "status_unavailable", {"http": http}, payload)
    return answered(payload)


def _cmd_wait(client: WizardClient, args: argparse.Namespace) -> int:
    from jasper.active_speaker.round_bank import (  # lazy: banking imports analysis
        RoundBankError, bank_round,
    )

    from .round_views import run_bookkeeping  # lazy: wait-only view dispatch

    result = wait_for_round(client, run_id=args.run, timeout_s=args.timeout, poll_s=DEFAULT_POLL_S)
    if result["status"] != "terminal":
        return failed(EXIT_REFUSED if result["status"] == "failed" else EXIT_UNREADABLE,
                      str(result["reason"]), result)
    session_dir = _round_session_dir(args.run)
    if not session_dir:
        return failed(EXIT_UNREADABLE, "capture_bundle_unavailable", result)
    try:
        banked = bank_round(Path(session_dir), view_runner=run_bookkeeping)
    except RoundBankError as exc:
        return failed(EXIT_REFUSED, exc.reason, str(exc))
    except OSError as exc:
        return failed(EXIT_WRITE_FAILED, "write_failed", str(exc))
    return _answer("wait", f"Run banked at {banked.path}", run_id=args.run,
                   result=result.get("result"), round_dir=str(banked.path),
                   manifest=banked.provenance.get("manifest"),
                   views=banked.provenance.get("views", []))


def _cmd_apply(client: WizardClient, args: argparse.Namespace) -> int:
    result = apply_by_fingerprint(client, args.expected_fingerprint)
    if result["refused_by"] == "client" and result["reason"] != REASON_NO_FINGERPRINT:
        http, payload = client.post_json(
            REPUBLISH_PATH, {"fingerprint": args.expected_fingerprint.strip()},
        )
        if http != 200 or not isinstance(payload, dict) or payload.get("status") != "republished":
            return _wizard_failure(
                EXIT_UNREADABLE if http == 0 else EXIT_REFUSED,
                REASON_ANSWER_LOST if http == 0 else "candidate_not_republished",
                {"http": http}, payload,
            )
        result = apply_by_fingerprint(client, args.expected_fingerprint)
    fingerprint = str(result["candidate_fingerprint"])
    if result["status"] == "applied":
        return _answer(
            "apply",
            f"applied {fingerprint}",
            candidate_fingerprint=fingerprint,
            http=result["http"],
            outcome=result["outcome"],
        )
    lost = result["reason"] == REASON_ANSWER_LOST
    return _wizard_failure(
        EXIT_UNREADABLE if lost else EXIT_REFUSED, str(result["reason"]),
        {
            "refused_by": result["refused_by"],
            "expected_candidate_fingerprint":
                result["expected_candidate_fingerprint"],
            "candidate_fingerprint": fingerprint,
            "http": result["http"],
            "outcome": result["outcome"],
            **({"advice": LOST_ANSWER_ADVICE} if lost else {}),
        },
        result["payload"] if result["payload"] is not None
        else "refused before any request left this speaker",
    )


def _connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hostname", help="speaker hostname used for the Host header")
    parser.add_argument("--base-url", default="http://127.0.0.1", help="wizard address")


def _timeout(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("timeout must be finite and nonnegative")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="resolve and post a plan; return its handoff link immediately")
    _connection_args(run)
    run.add_argument("--program", choices=("speaker", "room", "bass"))
    poses = run.add_mutually_exclusive_group()
    poses.add_argument("--poses", help="named pose set or comma-separated bearings in degrees")
    poses.add_argument("--layout", dest="poses", help="named layout from the program registry")
    run.add_argument("--candidates", help="comma-separated fingerprints (or base); supplied means trial")
    run.add_argument("--level", type=float, help="reference volume in dB; must match the banked level")
    run.add_argument("--ceiling", "--ceiling-db-spl", dest="ceiling", type=float, help="SPL ceiling in dB SPL")
    run.add_argument("--repeats", type=int, help="takes per pose and configuration")
    run.add_argument("--mover", choices=("human", "arm", "confirmed"))
    run.add_argument("--plan", help="v3 plan document; used without plan-building flags")
    run.add_argument("--dry-run", action="store_true", help="print preflight; play nothing")
    run.set_defaults(func=_cmd_run)
    for verb, function in (("placed", _cmd_placed), ("status", _cmd_status), ("wait", _cmd_wait)):
        command = sub.add_parser(verb, help=function.__name__.removeprefix("_cmd_"))
        _connection_args(command)
        command.add_argument("--run", required=True, help="run id returned by run")
        if verb == "placed":
            command.add_argument("--pose", type=int, help="expected pending pose number")
        if verb == "wait":
            command.add_argument("--timeout", "--timeout-s", type=_timeout, default=DEFAULT_TIMEOUT_S, help="wait limit in seconds")
        command.set_defaults(func=function)
    apply = sub.add_parser("apply", help="apply the named banked candidate")
    _connection_args(apply)
    apply.add_argument("--expected-fingerprint", required=True)
    apply.set_defaults(func=_cmd_apply)
    return parser


def main(argv: Sequence[str] | None = None, *, opener: Any | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = WizardClient(host_header=args.hostname or read_identity().hostname,
                          base_url=args.base_url, csrf_page_path=CSRF_PAGE_PATH, opener=opener)
    return int(args.func(client, args))


if __name__ == "__main__":
    raise SystemExit(main())
