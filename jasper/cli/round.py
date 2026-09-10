# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Open, wait, apply a named candidate, bank a round through existing owners, or plan one.

Apply republishes a banked selection only when it differs from the live slot;
the existing wizard apply path owns graph admission, installation and restore.
``plan`` sequences nothing either: it writes a measurement plan document that
no other verb here reads yet.
"""

from __future__ import annotations

import argparse
import shlex
from pathlib import Path
from typing import Any, Sequence

from jasper.active_speaker.wizard_client import (
    CSRF_PAGE_PATH,
    REASON_ANSWER_LOST,
    REASON_NO_FINGERPRINT,
    SESSION_PATH,
    STAGE_MEASURE,
    STAGE_POST_APPLY,
    TIERS,
    VERIFY_PATH,
    WizardClient,
    apply_by_fingerprint,
    error_of,
    wait_for_round,
)
from jasper.identity.reader import CROSSOVER_PAGE_PATH, read_identity, speaker_url

from ._refusal import (
    EXIT_OK as EXIT_OK,
    EXIT_REFUSED, EXIT_UNREADABLE, EXIT_WRITE_FAILED, answered, failed,
    read_json_source,
)
from ._report import write_report

PROG = "jasper-round"
REPUBLISH_PATH = "/sound/speaker/crossover/v2/republish"
DEFAULT_TIMEOUT_S = 900.0
DEFAULT_POLL_S = 5.0

#: This tool's own refusals, in the slug vocabulary the library's carry.
REASON_TIER_REQUIRED = "tier_required"
REASON_OPEN_REFUSED = "open_refused"

#: Said whenever an apply's answer is lost, because "it failed" is a claim this
#: tool cannot make there.
LOST_ANSWER_ADVICE = (
    "the apply may or may not have taken effect -- read the crossover status "
    "and decide from the live candidate, not from this exit code"
)

#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204).
AUTHORITY_TIER = "mutating-with-gates (`open`/`apply`/`bank` write; `wait` does not)"

#: A lost answer and a deadline are both UNREADABLE: neither is a refusal and
#: neither says the round failed. Which one it was rides in the receipt's
#: ``reason`` (``answer_lost`` / ``wait_timeout``), not in the number.
_EXIT_BY_WAIT_STATUS = {
    "failed": EXIT_REFUSED,
    "lost": EXIT_UNREADABLE,
    "timed_out": EXIT_UNREADABLE,
}


def _answer(verb: str, human: str, **fields: Any) -> int:
    """The verb's answer: its non-empty fields, under the verb."""

    return answered(
        {"verb": verb, **{k: v for k, v in fields.items() if v not in (None, "", {})}},
        human,
    )


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


def _prescription_doors(args: argparse.Namespace) -> dict[str, Any]:
    """The alignment and topology documents, under the keys the host reads.

    Passed through as read: the gate that judges a prescription is the session
    open's own, and a second one here would be a weaker copy of it.
    """
    # The two key constants' modules pull numpy and scipy in, which is why both
    # the guard and the deferred import are here: an ordinary open must not pay
    # for a door it is not carrying (ADR-0226).
    if not (args.alignment_prescription or args.topology_prescription):
        return {}
    from jasper.active_speaker.crossover_v2.alignment_prescription import (
        ALIGNMENT_PRESCRIPTION_KEY,
    )
    from jasper.active_speaker.crossover_v2.topology_prescription import (
        TOPOLOGY_PRESCRIPTION_KEY,
    )

    return {
        key: read_json_source(path)
        for key, path in (
            (ALIGNMENT_PRESCRIPTION_KEY, args.alignment_prescription),
            (TOPOLOGY_PRESCRIPTION_KEY, args.topology_prescription),
        )
        if path
    }


def _cmd_open(client: WizardClient, args: argparse.Namespace) -> int:
    """One stage open. The tier is stated, never inherited (#2639).

    An ABSENT tier resolves server-side to ``full``, silently demoting an
    Express household and handing the turntable rig a plan it cannot walk. So
    the measuring stage refuses without one here rather than posting a body
    whose meaning depends on what the last session was. The post-apply stage
    takes no tier at all: it reads the instrument the MEASURING session
    recorded.
    """
    post_apply = args.stage == STAGE_POST_APPLY
    if not post_apply and not args.tier:
        return failed(EXIT_REFUSED, REASON_TIER_REQUIRED, {"stage": args.stage})
    try:
        prescriptions = {} if post_apply else _prescription_doors(args)
    except ValueError as exc:
        return failed(
            EXIT_REFUSED, REASON_OPEN_REFUSED,
            {"stage": args.stage, "error": str(exc)},
        )
    path = VERIFY_PATH if post_apply else SESSION_PATH
    http, payload = client.open_session(
        args.tier or "", stage=args.stage, prescriptions=prescriptions
    )
    if http != 200:
        return failed(
            EXIT_UNREADABLE if http == 0 else EXIT_REFUSED,
            REASON_ANSWER_LOST if http == 0 else REASON_OPEN_REFUSED,
            {"stage": args.stage, "tier": args.tier or "", "path": path,
             "http": http, "error": error_of(payload)},
        )
    block = client.v2_block()
    url = speaker_url(CROSSOVER_PAGE_PATH)
    return _answer(
        "open",
        f"{args.stage} session open -- the round is driven at {url}",
        stage=args.stage,
        tier=None if post_apply else args.tier,
        path=path,
        http=http,
        session_id=str(block.get("session_id") or ""),
        phase=str(block.get("phase") or ""),
        handoff_url=url,
        next=f"{PROG} wait",
    )


def _cmd_wait(client: WizardClient, args: argparse.Namespace) -> int:
    result = wait_for_round(
        client, timeout_s=args.timeout_s, poll_s=args.poll_s
    )
    status = str(result["status"])
    facts = {key: result.get(key) for key in ("capture", "needs_recovery", "execution", "verify")}
    if status != "terminal":
        return failed(
            _EXIT_BY_WAIT_STATUS[status], str(result["reason"]),
            {**facts, "phase": result["phase"], "session_id": result["session_id"],
             "failure": result["failure"],
             "waited_s": args.timeout_s if status == "timed_out" else None},
        )
    session_dir = _round_session_dir(str(result["session_id"]))
    return _answer(
        "wait",
        f"session {result['session_id']} stopped at {result['phase']}",
        phase=result["phase"],
        session_id=result["session_id"],
        candidate_fingerprint=result["candidate_fingerprint"],
        session_dir=session_dir,
        next=f"{PROG} bank {shlex.quote(session_dir)}" if session_dir else "",
        session_dir_reason="" if session_dir else "capture_bundle_unavailable",
        **facts,
    )


def _cmd_apply(client: WizardClient, args: argparse.Namespace) -> int:
    result = apply_by_fingerprint(client, args.expected_fingerprint)
    if result["refused_by"] == "client" and result["reason"] != REASON_NO_FINGERPRINT:
        http, payload = client.post_json(
            REPUBLISH_PATH, {"fingerprint": args.expected_fingerprint.strip()},
        )
        if http != 200 or not isinstance(payload, dict) or payload.get("status") != "republished":
            return failed(
                EXIT_UNREADABLE if http == 0 else EXIT_REFUSED,
                REASON_ANSWER_LOST if http == 0 else "candidate_not_republished",
                {"http": http, "error": error_of(payload)},
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
    return failed(
        EXIT_UNREADABLE if lost else EXIT_REFUSED, str(result["reason"]),
        {
            "refused_by": result["refused_by"],
            "expected_candidate_fingerprint":
                result["expected_candidate_fingerprint"],
            "candidate_fingerprint": fingerprint,
            "http": result["http"],
            "outcome": result["outcome"],
            "error": (
                error_of(result["payload"]) if result["payload"] is not None
                else "refused before any request left this speaker"
            ),
            **({"advice": LOST_ANSWER_ADVICE} if lost else {}),
        },
    )


def _cmd_bank(args: argparse.Namespace) -> int:
    # Banking pulls the whole bundle and measurement import graph in: an
    # ordinary open must not pay for a door it is not carrying (ADR-0226).
    from jasper.active_speaker.round_bank import (
        DEFAULT_CAMPAIGN_ROOT,
        RoundBankError,
        bank_round,
    )

    root = Path(args.campaign_root) if args.campaign_root else DEFAULT_CAMPAIGN_ROOT
    try:
        banked = bank_round(Path(args.session_dir), campaign_root=root)
    except RoundBankError as exc:
        return failed(EXIT_REFUSED, exc.reason, str(exc))
    except OSError as exc:
        return failed(EXIT_WRITE_FAILED, "write_failed", str(exc))
    provenance = banked.provenance
    return _answer(
        "bank",
        f"{banked.path} session={provenance['session_id']} "
        f"banked_at_utc={provenance['banked_at_utc']} "
        f"installed_sha={provenance['installed_sha'] or 'unknown'} "
        f"missing={','.join(provenance['missing'] or ['none'])}",
        round_dir=str(banked.path),
        provenance=provenance,
    )


def _hz_band(value: str) -> tuple[float, float]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("--band must be LOW,HIGH in Hz")
    try:
        return (float(parts[0]), float(parts[1]))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--band must be numeric LOW,HIGH in Hz, got {value!r}"
        ) from exc


def _cmd_plan(args: argparse.Namespace) -> int:
    """Write a measurement plan document. Reaches no wizard; stages nothing.

    The plan model pulls in ``angle_capture``'s mover vocabulary, which costs
    numpy on import, so every name it needs is resolved here rather than at
    module scope -- an ordinary ``open``/``wait``/``apply``/``bank`` must not
    pay for a door it is not carrying (ADR-0226).
    """
    from jasper.active_speaker.candidate_bank import (
        CandidateBankRefusal,
        find_banked_candidate,
    )
    from jasper.active_speaker.crossover_v2.measurement_plan import (
        LevelPolicy,
        PlanRefusal,
        plan_for_program,
    )
    from jasper.active_speaker.measured_crossover_candidate import candidate_trial_scope

    candidates = tuple(
        field.strip() for field in args.candidates.split(",") if field.strip()
    )
    root = Path(args.root) if args.root else None
    candidate_scopes: dict[str, Any] = {}
    try:
        for candidate in candidates:
            if candidate == "base":
                continue
            banked = find_banked_candidate(candidate, root=root)
            candidate_scopes[candidate] = candidate_trial_scope(banked.candidate)
    except CandidateBankRefusal as exc:
        return failed(EXIT_REFUSED, exc.code, exc.detail)

    level = LevelPolicy(
        mode=args.level_mode, main_volume_series_db=tuple(args.level_series or ())
    )
    try:
        plan = plan_for_program(
            args.program, args.size,
            candidates=candidates, candidate_scopes=candidate_scopes,
            purpose=args.purpose, mover=args.mover,
            sweep_band_hz=args.band, sweep_s=args.sweep_s,
            level=level, ceiling_db_spl=args.ceiling_db_spl,
        )
        plan.validate()
    except PlanRefusal as exc:
        return failed(EXIT_REFUSED, exc.code, str(exc))
    except ValueError as exc:
        return failed(EXIT_REFUSED, "plan_refused", str(exc))

    try:
        written = write_report(plan.to_dict(), args.out, Path(args.out))
    except OSError as exc:
        return failed(EXIT_WRITE_FAILED, "write_failed", str(exc))
    if written is None:
        # --out - already put the plan itself on stdout (ADR-0237); a second
        # document over it would break "exactly one".
        return EXIT_OK
    return _answer(
        "plan",
        f"{written} fingerprint={plan.fingerprint} "
        f"poses={len(plan.poses)} takes={len(plan.takes)}",
        out=str(written),
        fingerprint=plan.fingerprint,
        cost=plan.cost().to_dict(),
        poses=len(plan.poses),
        takes=len(plan.takes),
    )


def _connection_args(parser: argparse.ArgumentParser) -> None:
    """The wizard verbs' shared arguments; ``bank`` reaches no wizard."""
    parser.set_defaults(wizard=True)
    parser.add_argument(
        "--hostname",
        default=None,
        help=(
            "the speaker's own hostname (JASPER_HOSTNAME, e.g. jts3.local). "
            "Sent as the Host header so the wizard's management-host guard "
            "admits a loopback request -- it refuses 127.0.0.1, and it "
            "refuses another speaker's name (default: this speaker's "
            "configured identity)"
        ),
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1",
        help="where the wizard is reached (default: %(default)s)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Open, wait on, apply and bank a crossover round from the speaker "
            "itself. The three wizard verbs scripts/run-crossover-round.py "
            "drives from a laptop, over the same transport and the same apply "
            "gate, plus the bank that files a finished session in the on-box "
            "campaign home."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "EXAMPLE\n"
            "  ssh pi@jts3.local\n"
            "  /opt/jasper/.venv/bin/jasper-round plan --program room "
            "--candidates base\n"
            "  /opt/jasper/.venv/bin/jasper-round open --tier express\n"
            "  /opt/jasper/.venv/bin/jasper-round wait --timeout-s 1200\n"
            "  /opt/jasper/.venv/bin/jasper-round apply "
            "--expected-fingerprint <fp>\n"
            "  /opt/jasper/.venv/bin/jasper-round bank <session-dir>\n"
            "\n"
            "WHAT THIS DOES NOT DO\n"
            "  - it does not stage an angle walk or run the arm (both are\n"
            "    jasper-angle-capture); each is its own tool and this one\n"
            "    sequences none of them\n"
            "  - `plan` writes a measurement plan document; no verb here\n"
            "    reads it yet, so it stages no walk and opens no session\n"
            "  - `wait` polls the session the wizard is publishing NOW, so\n"
            "    run it after `open`, not against yesterday's round\n"
            "  - `bank` files a round on this box only -- the same tree is\n"
            "    assembled over ssh by scripts/bank-crossover-round.sh -- and\n"
            "    evicts nothing: the campaign home is operator-pruned\n"
            "\n"
            "EXIT CODES\n"
            "  0  the verb did what it says -- the wizard answered, the\n"
            "     round was banked and its directory is on stdout, or the\n"
            "     plan was written and its summary is on stdout\n"
            "  1  EXIT_REFUSED -- the wizard's refusal, this tool's own\n"
            "     pre-flight fingerprint or plan refusal, or a session\n"
            "     `bank` will not bank (not a bundle, unfinished, or\n"
            "     already banked -- a banked round is never overwritten).\n"
            "     Nothing was applied\n"
            "  2  EXIT_UNREADABLE -- no answer to read. The receipt's\n"
            "     `reason` says which: `answer_lost` (the daemon is down, a\n"
            "     wrong --hostname, a dropped connection) or `wait_timeout`\n"
            "     (the deadline passed with the session still running --\n"
            "     nothing was cancelled, the round is still going). A lost\n"
            "     answer to the apply POST does NOT mean the apply failed\n"
            "  3  EXIT_WRITE_FAILED -- `bank` could not write the copy, or\n"
            "     `plan` could not write its document: a filesystem\n"
            "     problem, not a request problem"
        ),
    )
    parser.set_defaults(wizard=False)
    sub = parser.add_subparsers(dest="command", required=True)

    opener = sub.add_parser("open", help="post one stage open on this speaker")
    _connection_args(opener)
    opener.add_argument(
        "--tier",
        choices=sorted(TIERS),
        default=None,
        help=(
            "the commission instrument this session measures with. Required "
            "for --stage %s and ignored for %s, which takes the instrument "
            "the measuring session recorded"
            % (STAGE_MEASURE, STAGE_POST_APPLY)
        ),
    )
    opener.add_argument(
        "--stage",
        choices=(STAGE_MEASURE, STAGE_POST_APPLY),
        default=STAGE_MEASURE,
        help=(
            "%s opens a new measuring session; %s opens the post-apply check "
            "(default: %%(default)s)" % (STAGE_MEASURE, STAGE_POST_APPLY)
        ),
    )
    for door in ("alignment", "topology"):
        opener.add_argument(
            f"--{door}-prescription",
            metavar="PATH",
            default=None,
            help=(
                "a JSON document -- a file, or - for stdin -- posted verbatim "
                f"as this session's {door} prescription. The open's own gate "
                f"judges it, never this tool; ignored by --stage {STAGE_POST_APPLY}"
            ),
        )
    opener.set_defaults(func=_cmd_open)

    waiter = sub.add_parser(
        "wait", help="poll until the wizard's session stops; writes nothing"
    )
    _connection_args(waiter)
    waiter.add_argument(
        "--timeout-s", type=float, default=DEFAULT_TIMEOUT_S,
        help="how long the session may take to stop (default: %(default)s)",
    )
    waiter.add_argument(
        "--poll-s", type=float, default=DEFAULT_POLL_S,
        help="how often the envelope is read (default: %(default)s)",
    )
    waiter.set_defaults(func=_cmd_wait)

    applier = sub.add_parser(
        "apply", help="apply the candidate with THIS fingerprint and no other"
    )
    _connection_args(applier)
    applier.add_argument(
        "--expected-fingerprint",
        required=True,
        help=(
            "select this banked candidate, then use the full apply path; "
            "authored candidates need a completed trial of the same fingerprint"
        ),
    )
    applier.set_defaults(func=_cmd_apply)

    banker = sub.add_parser(
        "bank", help="file a finished session in the on-box campaign home"
    )
    banker.add_argument(
        "session_dir",
        help="the live session bundle to bank (the directory holding info.json)",
    )
    banker.add_argument(
        "--campaign-root",
        default=None,
        help="where banked rounds live (default: the on-box campaign home)",
    )
    banker.set_defaults(func=_cmd_bank)

    planner = sub.add_parser(
        "plan", help="write a measurement plan document; stages and runs nothing"
    )
    planner.add_argument("--program", required=True, help="measurement program id, e.g. room")
    planner.add_argument(
        "--size", default=None,
        help="program size; the program's own default when omitted",
    )
    planner.add_argument(
        "--candidates", required=True,
        help=(
            "comma-separated take list: 'base' and/or a banked candidate "
            "fingerprint, e.g. base,<fingerprint>"
        ),
    )
    planner.add_argument(
        "--purpose", default=None,
        help="capture purpose; the program's own default when omitted",
    )
    planner.add_argument(
        # Literal choices, not MOVER_HUMAN/MOVER_ARM: importing .angle_capture
        # here would tax every verb's startup, not just plan's (ADR-0226).
        "--mover", default="human", choices=("human", "arm"),
        help="who advances the mic between poses (default: %(default)s)",
    )
    planner.add_argument(
        "--sweep-s", type=float, default=None,
        help="summed sweep duration for every take, in seconds",
    )
    planner.add_argument(
        "--band", type=_hz_band, default=None, metavar="LOW,HIGH",
        help="sweep band bounds in Hz for every take",
    )
    planner.add_argument(
        "--ceiling-db-spl", type=float, default=None,
        help="one SPL ceiling for every take",
    )
    planner.add_argument(
        # Literal default, not LEVEL_HOLD_REFERENCE: same import-cost reason
        # as --mover above; a bad value comes back as `unknown_level_mode`.
        "--level-mode", default="hold_reference",
        help="how the executor holds main volume across takes (default: %(default)s)",
    )
    planner.add_argument(
        # Repeatable, not a comma list: argparse reads a lone "-20,-30" as an
        # unrecognized option (the embedded comma defeats its negative-number
        # exemption), the same reason measure.py's --level-dbfs repeats too.
        "--level-series", type=float, action="append", default=None, metavar="DB",
        help=(
            "one main-volume rung for --level-mode series, repeatable "
            "(e.g. --level-series -20 --level-series -30)"
        ),
    )
    planner.add_argument(
        "--out", default="measurement_plan.json",
        help="where the plan document is written (default: %(default)s)",
    )
    planner.add_argument(
        "--root", default=None,
        help="the candidate bank root, when it is not the on-box default",
    )
    planner.set_defaults(func=_cmd_plan)
    return parser


def main(argv: Sequence[str] | None = None, *, opener: Any | None = None) -> int:
    """``opener`` is :class:`WizardClient`'s own transport seam, for tests.

    ``bank`` reaches no wizard, so no client is built -- and it declares none
    of :func:`_connection_args`' arguments to build one from.
    """
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if not args.wizard:
        return int(args.func(args))
    client = WizardClient(
        host_header=args.hostname or read_identity().hostname,
        base_url=args.base_url,
        csrf_page_path=CSRF_PAGE_PATH,
        opener=opener,
    )
    return int(args.func(client, args))


if __name__ == "__main__":  # pragma: no cover - console-script entry point
    raise SystemExit(main())
