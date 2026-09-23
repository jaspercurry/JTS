# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Run a plan, bank its packet, list and show banked rounds, commission a speaker and apply candidates."""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import urlsplit

from jasper.net.http_security import _is_loopback_name
from jasper.json_fields import age_seconds, parse_utc_iso

from jasper.audio_measurement.evidence_reasons import REASON_UNREADABLE
from jasper.active_speaker.measurement_programs import RUNNABLE_PROGRAMS
from jasper.active_speaker.movers import MOVER_ARM, MOVERS
from jasper.active_speaker.round_copy import round_lines, packet_lines
from jasper.active_speaker.wizard_client import (
    CSRF_PAGE_PATH, STATUS_PATH, REASON_ANSWER_LOST,
    WizardClient, apply_by_fingerprint, error_of, wait_for_round,
)
from jasper.identity.reader import CROSSOVER_PAGE_PATH, speaker_url
from jasper.logging_setup import configure_logging

from ._refusal import (
    EXIT_OK as EXIT_OK,
    EXIT_REFUSED, EXIT_UNREADABLE, EXIT_WRITE_FAILED, answered, failed,
)

PROG = "jasper-round"
DEFAULT_TIMEOUT_S = 900.0
AUTHORITY_TIER = "mutating-with-gates (`run`/`trial`/`placed`/`stop`/`wait`/`apply`/`reset` write; `run`/`trial` may move the arm; `status`/`list`/`show` read)"
LOST_ANSWER_ADVICE = "the apply may have taken effect; read the live candidate before trying again"
_BEARING_LIST = re.compile(r"-\d+(\.\d+)?(,[+-]?\d+(\.\d+)?)*")


def _joined_bearings(args: Iterable[str]) -> list[str]:
    """Rewrite ``--poses <list>`` as ``--poses=<list>``.

    argparse reads a lone negative number as a value but not a bearing list
    that starts with one, so the spaced spelling would be an unknown option.
    """
    joined: list[str] = []
    for arg in args:
        if joined[-1:] == ["--poses"] and _BEARING_LIST.fullmatch(arg):
            joined[-1] = f"--poses={arg}"
        else:
            joined.append(arg)
    return joined


class _RoundSubparser(argparse.ArgumentParser):
    def format_help(self) -> str:
        if self.prog.endswith(" reset"):
            from jasper.active_speaker.crossover_v2.refusal_copy import TIMING_RESET_NOTE  # lazy: help-only operator copy
            self.description = TIMING_RESET_NOTE
        return super().format_help()

    def parse_known_args(self, args: Iterable[str] | None = None, namespace: Any = None) -> Any:
        return super().parse_known_args(args if args is None else _joined_bearings(args), namespace)


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


def _run_links(run_id: str) -> dict[str, str]:
    return dict(run_id=run_id, link=speaker_url(CROSSOVER_PAGE_PATH), status_url=speaker_url(STATUS_PATH))


def _cmd_run(client: WizardClient, args: argparse.Namespace) -> int:
    from jasper.active_speaker.crossover_v2.contracts import CrossoverV2FlowError  # lazy: run-only
    from ._run_request import ARM_FACT_CODES, resolve_run  # lazy: run-only measurement imports

    try:
        report = resolve_run(args)
    except PermissionError as exc:
        return failed(EXIT_REFUSED, "local_state_unreadable", {"evidence": {"path": exc.filename}},
                      code="local_state_unreadable", next_action={
                          "id": "run_as_root", "label": "run on the speaker as root (`sudo -n`)",
                      })
    except (ValueError, OSError, CrossoverV2FlowError) as exc:
        return failed(EXIT_REFUSED, getattr(exc, "reason", "program_plan_shape_invalid"), str(exc))
    if report.plan.mover == MOVER_ARM and not args.wait and not args.dry_run:
        build_parser().error("--mover arm requires --wait")
    if args.dry_run:
        answered({"verb": "run", "dry_run": args.dry_run, **report.to_dict()})
        return EXIT_REFUSED if report.blocking else EXIT_OK
    refusal = next((issue for issue in report.issues if issue.blocking and issue.code in ARM_FACT_CODES), None)
    if refusal is not None:
        return failed(EXIT_REFUSED, refusal.code, report.to_dict(), code=refusal.code, next_action=refusal.next_action)
    http, payload = client.open_session(report.plan.to_dict())
    if http != 200:
        return _wizard_failure(EXIT_UNREADABLE if http == 0 else EXIT_REFUSED,
                               "run_refused", {"http": http}, payload)
    capture = payload.get("capture", payload) if isinstance(payload, dict) else None
    run_id = capture.get("session_id") if isinstance(capture, dict) else None
    if not isinstance(capture, dict) or not isinstance(run_id, str) or not run_id:
        return failed(EXIT_UNREADABLE, "run_answer_invalid", payload)
    if args.wait:
        print(json.dumps(_run_links(run_id)), file=sys.stderr, flush=True)
        args.run = run_id
        if report.plan.mover != MOVER_ARM:
            return _cmd_wait(client, args)
        from jasper.active_speaker import arm_walk  # lazy: arm-only

        configure_logging()
        arm_walk.install_park_on_signals()
        with arm_walk.RunOwnedArm(
            arm_walk.TurntableMover(attest_rig_clear=args.attest_rig_clear),
            arm_walk.LoopbackSession(host_header=args.hostname, base_url=args.base_url),
            arm_walk.WalkConfig(),
        ) as arm:
            return _cmd_wait(client, args, finish_arm=arm.finish)
    return _answer(args.command, "Run ready; place the microphone to start.",
                   **_run_links(run_id),
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
    selected = run_program("speaker") if declared else trial_program(sections, args.mover)
    args.program, args.poses = selected.program_id, selected.layout
    if not declared: args.mover = selected.mover
    args.candidates = args.candidates or (None if declared else f"base,{banked.fingerprint}")
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
    for line in (packet_lines(payload["round_dir"]) if payload.get("round_dir") else
                 round_lines(payload, pending=bool(payload.get("pending")))):
        print(line, file=sys.stderr)
    return answered(payload, (f"session level {session['leveled_db_spl']:.1f} dB SPL at gain {session['gain_db']:.1f} dB, "
                              f"leveled {age_seconds(stamp) / 3600:.1f}h ago, reused") if session and stamp is not None else "")


def _cmd_wait(client: WizardClient, args: argparse.Namespace, *,
              finish_arm: Callable[[], dict[str, Any]] = lambda: {}) -> int:
    from jasper.active_speaker.round_bank import (  # lazy: banking imports analysis
        RoundBankError, finish_round,
    )
    from jasper.active_speaker.round_packet import wait_answer  # lazy: wait-only packet assembly

    previous_lines: list[str] = []
    def show_progress(progress):
        nonlocal previous_lines
        lines = round_lines(progress, pending=bool(progress.get("pending")))
        if lines != previous_lines:
            print("\n".join(lines), file=sys.stderr)
            previous_lines = lines
    result = wait_for_round(client, run_id=args.run, timeout_s=args.timeout, on_progress=show_progress)
    arm = finish_arm()
    result.update(arm)
    if arm.get("arm", {}).get("exit") == "arm_park_unconfirmed":
        return failed(EXIT_UNREADABLE, "arm_park_unconfirmed", result, code="arm_park_unconfirmed")
    if result["status"] != "terminal":
        return failed(EXIT_REFUSED if result["status"] == "failed" else EXIT_UNREADABLE,
                      str(result["reason"]), result)
    if result.get("captured") is False:
        return failed(EXIT_REFUSED, str(result.get("code") or "run_not_live"), result)
    session_dir = _round_session_dir(args.run)
    if not session_dir:
        return failed(EXIT_UNREADABLE, "capture_bundle_unavailable", result)
    banked, error = finish_round(Path(session_dir))
    if banked is None:
        return failed(EXIT_REFUSED if isinstance(error, RoundBankError) else EXIT_WRITE_FAILED,
                      error.reason if isinstance(error, RoundBankError) else "write_failed",
                      {"error": str(error), **arm} if arm else str(error))
    return answered({**wait_answer(banked, result, verbose=args.verbose), **_run_links(args.run), **arm},
                    "\n".join([f"Run banked at {banked.path}", *packet_lines(str(banked.path))]), sort_keys=False)


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


def _cmd_reset(client: WizardClient, args: argparse.Namespace) -> int:
    from jasper.active_speaker.baseline_profile import load_applied_baseline_profile_state  # lazy: reset-only state
    from jasper.active_speaker.candidate_bank import (  # lazy: reset-only bank
        CandidateBankRefusal, publish_authored_candidate,
    )
    from jasper.active_speaker.crossover_v2.prescription_document import (  # lazy: reset-only prescription stack imports NumPy
        REASON_EVIDENCE_UNREADABLE, PrescriptionDocumentRefused, judge_prescription_document,
        reset_prescription_document, saved_base,
    )
    from jasper.active_speaker.crossover_v2.refusal_copy import refusal_copy_for  # lazy: refusal copy imports NumPy
    from jasper.audio_measurement.bundles import BundleError  # lazy: reset-only bank writer

    try:
        base, applied = saved_base()
        corrections = applied.get("corrections") or {}
        trims_db = {role: values["gain_db"] for role, values in corrections.items()}
        document = reset_prescription_document(
            keep_timing=args.keep_timing, trims_db=trims_db, program=args.program,
        )
        candidate = judge_prescription_document(document, base=base, base_profile=applied)
        published = publish_authored_candidate(candidate)
    except PrescriptionDocumentRefused as exc:
        return failed(EXIT_UNREADABLE if exc.code == REASON_EVIDENCE_UNREADABLE else EXIT_REFUSED, exc.code,
                      exc.failure_detail(), code=exc.code, next_action=refusal_copy_for(exc.code)[1])
    except (CandidateBankRefusal, BundleError, OSError, TypeError, ValueError) as exc:
        return failed(EXIT_UNREADABLE, "reset_compose_failed", str(exc))
    result = apply_by_fingerprint(client, published.fingerprint)
    fingerprint = str(result["candidate_fingerprint"])
    if result["status"] != "applied":
        return _wizard_failure(EXIT_REFUSED, str(result["reason"]),
                               {"candidate_fingerprint": fingerprint}, result.get("payload"))
    timing = (load_applied_baseline_profile_state() or {}).get("timing")
    return _answer(
        "reset", f"applied {fingerprint}", candidate_fingerprint=fingerprint,
        http=result["http"], outcome=result["outcome"], trims_db=trims_db,
        timing={"saved": timing is not None,
                "provenance": timing.get("provenance") if isinstance(timing, dict) else None},
    )


def _cmd_list(args: argparse.Namespace) -> int:
    from jasper.active_speaker.round_bank import list_rounds  # lazy: keeps the CLI parser numpy-free

    rows = list_rounds(program=args.program, limit=args.limit + 1)
    shown = rows[:args.limit]
    return _answer("list", f"{len(shown)} banked round(s)" + (f", newest {shown[0]['round_id']}" if shown else ""),
                   rounds=shown, truncated=len(rows) > args.limit)


def _cmd_show(args: argparse.Namespace) -> int:
    from jasper.active_speaker.crossover_v2.round_inputs import ROUND_INPUT_ERRORS, RoundSetRefused  # lazy: keeps the CLI parser numpy-free
    from jasper.active_speaker.round_bank import RoundBankError, show_round  # lazy: keeps the CLI parser numpy-free

    try:
        shown = show_round(args.round)
    except RoundBankError as exc:
        return failed(EXIT_UNREADABLE, exc.reason, str(exc))
    except RoundSetRefused as exc:
        return failed(EXIT_REFUSED, exc.reason, exc.detail)
    except ROUND_INPUT_ERRORS as exc:
        return failed(EXIT_UNREADABLE, getattr(exc, "code", REASON_UNREADABLE), str(exc))
    takes = sum(len(group["takes"]) for group in shown["sets"])
    return _answer("show", f"{shown['round_id']}: {len(shown['sets'])} set(s), {takes} take(s)", **shown)


def _connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--hostname", help="Host header override (default: derived from --base-url)",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1", help="wizard address")


def _timeout(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("timeout must be finite and nonnegative")
    return result


def _limit(value: str) -> int:
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("limit must be at least 1")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_RoundSubparser)
    timeout_args = _RoundSubparser(add_help=False)
    timeout_args.add_argument("--verbose", action="store_true", help="include the banked view results")
    timeout_args.add_argument("--timeout", "--timeout-s", type=_timeout, default=DEFAULT_TIMEOUT_S, help="wait limit in seconds")
    run_args = _RoundSubparser(add_help=False, parents=[timeout_args])
    _connection_args(run_args)
    run_args.add_argument("--attest-rig-clear", action="store_true",
                          help="state that the arm's full sweep path is clear for this run")
    run_args.add_argument("--wait", action="store_true", help="wait for completion and bank the round with its packet")
    run_args.add_argument("--candidates", help="comma-separated fingerprints (or base); supplied means trial")
    run_args.add_argument("--level-db", type=float, help="one absolute run fader level in dB; overrides the program's level default")
    run = sub.add_parser("run", parents=[run_args], help="run a plan; optionally wait and bank its packet")
    run.add_argument("--program", choices=RUNNABLE_PROGRAMS)
    poses = run.add_mutually_exclusive_group()
    poses.add_argument("--poses", help="named pose set or comma-separated bearings in degrees")
    poses.add_argument("--layout", dest="poses", help="named layout from the program registry")
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
    for verb, function, help_line in (
        ("placed", _cmd_placed, "Confirm microphone placement at the pending pose."),
        ("stop", _cmd_stop, "Stop the current run."),
        ("status", _cmd_status, "Show run progress and the current microphone prompt."),
        ("wait", _cmd_wait, "Wait for the run to finish and bank its round packet."),
    ):
        command = sub.add_parser(verb, parents=[timeout_args] if verb == "wait" else [], help=help_line, description=help_line)
        _connection_args(command)
        command.add_argument("--run", required=True, help="run id returned by run")
        if verb == "placed":
            command.add_argument("--pose", type=int, help="expected pending pose number")
        command.set_defaults(func=function)
    apply = sub.add_parser("apply", help="apply the named banked candidate")
    _connection_args(apply)
    apply.add_argument("fingerprint", help="banked candidate fingerprint")
    apply.set_defaults(func=_cmd_apply)
    reset = sub.add_parser("reset", help="reset one program or all tuning")
    _connection_args(reset)
    reset.add_argument("--program", choices=RUNNABLE_PROGRAMS, help="reset only this program; omitted resets everything")
    reset.add_argument("--keep-timing", action="store_true", help="keep saved timing and its provenance (all tuning or speaker only)")
    reset.set_defaults(func=_cmd_reset)
    list_help = "List banked rounds, newest first: id, directory, program, status, sets and applied identity."
    listing = sub.add_parser("list", help=list_help, description=list_help)
    listing.add_argument("--program", choices=RUNNABLE_PROGRAMS, help="only rounds that count for this program")
    listing.add_argument("--limit", type=_limit, default=20, help="at most this many rounds (default 20)")
    listing.set_defaults(func=_cmd_list)
    show_help = "Show one round's sets and selected takes, by the ids every jasper-round-views verb accepts."
    show = sub.add_parser("show", help=show_help, description=show_help)
    show.add_argument("round", metavar="<round-id|path>", help="a banked round id from list, or a round directory")
    show.set_defaults(func=_cmd_show)
    return parser


def main(argv: Sequence[str] | None = None, *, opener: Any | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "reset" and args.keep_timing and args.program not in (None, "speaker"):
        parser.error("--keep-timing requires resetting everything or --program speaker")
    if args.command == "run" and args.dry_run and not _is_loopback_name(urlsplit(args.base_url).hostname or ""):
        from jasper.active_speaker.crossover_v2.refusal_copy import REASON_REGISTRY  # lazy: refused run copy
        return failed(EXIT_REFUSED, "dry_run_requires_local_host", REASON_REGISTRY["dry_run_requires_local_host"].message)
    if args.command in ("list", "show"):
        return int(args.func(args))
    client = WizardClient(
        host_header=args.hostname,
        base_url=args.base_url, csrf_page_path=CSRF_PAGE_PATH, opener=opener,
    )
    return int(args.func(client, args))


if __name__ == "__main__":
    raise SystemExit(main())
