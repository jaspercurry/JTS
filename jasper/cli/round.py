# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Run a plan across poses, graphs and levels, bank its packet, commission a speaker and apply candidates."""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

from jasper.net.http_security import _is_loopback_name
from jasper.json_fields import age_seconds, parse_utc_iso

from jasper.active_speaker.movers import MOVERS
from jasper.active_speaker.wizard_client import (
    CSRF_PAGE_PATH, STATUS_PATH, REASON_ANSWER_LOST,
    WizardClient, apply_by_fingerprint, error_of, wait_for_round,
)
from jasper.identity.reader import CROSSOVER_PAGE_PATH, read_identity, speaker_url

from ._refusal import (
    EXIT_OK as EXIT_OK,
    EXIT_REFUSED, EXIT_UNREADABLE, EXIT_WRITE_FAILED, answered, failed,
)

PROG = "jasper-round"
DEFAULT_TIMEOUT_S = 900.0
AUTHORITY_TIER = "mutating-with-gates (`run`/`trial`/`placed`/`stop`/`wait`/`apply` write; `status` reads)"
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
    except PermissionError as exc:
        return failed(EXIT_REFUSED, "local_state_unreadable", {"evidence": {"path": exc.filename}},
                      code="local_state_unreadable", next_action={
                          "id": "run_as_root", "label": "run on the speaker as root (`sudo -n`)",
                      })
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
    if args.wait:
        args.run = run_id
        return _cmd_wait(client, args)
    return _answer(args.command, "Run ready; place the microphone to start.",
                   run_id=run_id, link=speaker_url(CROSSOVER_PAGE_PATH),
                   status_url=speaker_url(STATUS_PATH),
                   shape="trial" if report.plan.candidates else "measure",
                   first_prompt=capture.get("first_prompt"), schedule=report.to_dict())


def _cmd_trial(client: WizardClient, args: argparse.Namespace) -> int:
    from jasper.active_speaker.candidate_bank import (  # lazy: trial-only candidate imports
        CandidateBankRefusal, find_banked_candidate,
    )
    from jasper.active_speaker.measurement_programs import run_program, trial_program  # lazy: trial-only
    from jasper.active_speaker.candidate_parts import DECLARED_CROSSOVER_PROGRAM_ID  # lazy: trial-only

    try:
        banked = find_banked_candidate(args.fingerprint)
    except CandidateBankRefusal as exc:
        return failed(EXIT_REFUSED, exc.code, exc.detail, code=exc.code)
    sections = sorted(name for name, source in banked.candidate.analysis.get("resolution", {}).items()
                      if source == "document")
    declared = banked.candidate.program_id == DECLARED_CROSSOVER_PROGRAM_ID
    if declared:
        sections = ["driver"]
    if len(sections) != 1:
        return failed(EXIT_REFUSED, "trial_sections_ambiguous", {"sections": sections}, code="trial_sections_ambiguous")
    selected = run_program("speaker") if declared else trial_program(sections[0], args.mover)
    args.program, args.poses, args.mover = selected.program_id, selected.layout, args.mover or selected.mover
    args.candidates = None if declared else f"base,{banked.fingerprint}"
    return _cmd_run(client, args)


def _cmd_placed(client: WizardClient, args: argparse.Namespace) -> int:
    http, payload = client.placed(args.run, args.pose)
    if http != 200:
        return _wizard_failure(EXIT_UNREADABLE if http == 0 else EXIT_REFUSED,
                               "placement_refused", {"run_id": args.run, "http": http}, payload)
    return answered(payload)


def _cmd_stop(client: WizardClient, args: argparse.Namespace) -> int:
    http, payload = client.stop(args.run)
    if http != 200:
        return _wizard_failure(EXIT_UNREADABLE if http == 0 else EXIT_REFUSED,
                               "stop_refused", {"run_id": args.run, "http": http}, payload)
    return answered(payload)


def _cmd_status(client: WizardClient, args: argparse.Namespace) -> int:
    http, payload = client.run_status(args.run)
    if http != 200:
        return _wizard_failure(EXIT_UNREADABLE if http == 0 else EXIT_REFUSED,
                               "status_unavailable", {"http": http}, payload)
    session = (payload.get("level") or {}).get("session")
    stamp = parse_utc_iso(session["leveled_at"]) if session else None
    return answered(payload, (f"session level {session['leveled_db_spl']:.1f} dB SPL at gain {session['gain_db']:.1f} dB, "
                              f"leveled {age_seconds(stamp) / 3600:.1f}h ago, reused") if session and stamp is not None else "")


def _cmd_wait(client: WizardClient, args: argparse.Namespace) -> int:
    from jasper.active_speaker.round_bank import (  # lazy: banking imports analysis
        RoundBankError, bank_round,
    )
    from .round_views import run_bookkeeping  # lazy: wait-only view dispatch
    from .round_views._bass_inputs import join_bass_rounds  # lazy: wait-only bass analysis
    from jasper.active_speaker.round_packet import finish_bass_packet, wait_answer  # lazy: wait-only packet assembly

    result = wait_for_round(client, run_id=args.run, timeout_s=args.timeout)
    if result["status"] != "terminal":
        return failed(EXIT_REFUSED if result["status"] == "failed" else EXIT_UNREADABLE,
                      str(result["reason"]), result)
    session_dir = _round_session_dir(args.run)
    if not session_dir:
        return failed(EXIT_UNREADABLE, "capture_bundle_unavailable", result)
    try:
        banked = bank_round(
            Path(session_dir), view_runner=run_bookkeeping,
        )
        manifest = banked.provenance.get("manifest")
        if manifest and Path(manifest).is_file():
            finish_bass_packet(banked.path, Path(manifest), join_levels=join_bass_rounds)
    except RoundBankError as exc:
        return failed(EXIT_REFUSED, exc.reason, str(exc))
    except OSError as exc:
        return failed(EXIT_WRITE_FAILED, "write_failed", str(exc))
    return answered(wait_answer(banked, result, verbose=args.verbose),
                    f"Run banked at {banked.path}", sort_keys=False)


def _cmd_apply(client: WizardClient, args: argparse.Namespace) -> int:
    result = apply_by_fingerprint(client, args.fingerprint)
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
    timeout_args = argparse.ArgumentParser(add_help=False)
    timeout_args.add_argument("--verbose", action="store_true", help="include the banked view results")
    timeout_args.add_argument("--timeout", "--timeout-s", type=_timeout, default=DEFAULT_TIMEOUT_S, help="wait limit in seconds")
    run_args = argparse.ArgumentParser(add_help=False, parents=[timeout_args])
    _connection_args(run_args)
    run_args.add_argument("--wait", action="store_true", help="wait for completion and bank the round with its packet")
    levels = run_args.add_mutually_exclusive_group()
    levels.add_argument("--levels", help="auto uses admissible session offsets; or comma-separated absolute dB levels")
    levels.add_argument("--level-db", type=float, help="one absolute run fader level in dB; bass otherwise uses auto levels")
    run = sub.add_parser("run", parents=[run_args], help="run a plan, with auto or explicit levels; optionally wait and bank its packet")
    run.add_argument("--program", choices=("speaker", "room", "bass"))
    poses = run.add_mutually_exclusive_group()
    poses.add_argument("--poses", help="named pose set or comma-separated bearings in degrees")
    poses.add_argument("--layout", dest="poses", help="named layout from the program registry")
    run.add_argument("--candidates", help="comma-separated fingerprints (or base); supplied means trial")
    run.add_argument("--repeats", type=int, help="takes per pose and configuration")
    run.add_argument("--mover", choices=MOVERS)
    run.add_argument("--plan", help="v5 plan document; used without plan-building flags")
    run.add_argument("--dry-run", action="store_true", help="read local facts and print preflight; run on the speaker with a loopback --base-url")
    run.set_defaults(func=_cmd_run)
    trial_help = "Test a banked candidate; declared crossovers start the speaker experiment."
    trial = sub.add_parser("trial", parents=[run_args], help=trial_help, description=trial_help)
    trial.add_argument("fingerprint", help="banked candidate fingerprint")
    trial.add_argument("--mover", choices=("arm", "human"))
    trial.set_defaults(func=_cmd_trial, plan=None, repeats=None, dry_run=False)
    for verb, function in (("placed", _cmd_placed), ("stop", _cmd_stop), ("status", _cmd_status), ("wait", _cmd_wait)):
        command = sub.add_parser(verb, parents=[timeout_args] if verb == "wait" else [], help=function.__name__.removeprefix("_cmd_"))
        _connection_args(command)
        command.add_argument("--run", required=True, help="run id returned by run")
        if verb == "placed":
            command.add_argument("--pose", type=int, help="expected pending pose number")
        command.set_defaults(func=function)
    apply = sub.add_parser("apply", help="apply the named banked candidate")
    _connection_args(apply)
    apply.add_argument("fingerprint", help="banked candidate fingerprint")
    apply.set_defaults(func=_cmd_apply)
    return parser


def main(argv: Sequence[str] | None = None, *, opener: Any | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for index in range(len(argv) - 1, 0, -1):
        if argv[index - 1] == "--levels" and argv[index].startswith("-") and "," in argv[index]:
            argv[index - 1:index + 1] = [f"--levels={argv[index]}"]
    args = build_parser().parse_args(argv)
    if args.command == "run" and args.dry_run and not _is_loopback_name(urlsplit(args.base_url).hostname or ""):
        from jasper.active_speaker.crossover_v2.refusal_copy import REASON_REGISTRY  # lazy: refused run copy
        return failed(EXIT_REFUSED, "dry_run_requires_local_host", REASON_REGISTRY["dry_run_requires_local_host"].message)
    client = WizardClient(host_header=args.hostname or read_identity().hostname,
                          base_url=args.base_url, csrf_page_path=CSRF_PAGE_PATH, opener=opener)
    return int(args.func(client, args))


if __name__ == "__main__":
    raise SystemExit(main())
