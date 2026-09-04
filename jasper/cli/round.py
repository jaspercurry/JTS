# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Run a crossover round -- open, wait, apply, bank -- from a shell on the speaker.

The round verbs the laptop's ``scripts/run-crossover-round.py`` already drove
over the LAN, reachable from the box itself. Same wizard, same transport
(:mod:`jasper.active_speaker.wizard_client`), same apply gate -- what changes is
only WHERE the operator is standing, which matters when there is no laptop on
the network, when the round is being driven from an ssh session, or when a
script on the speaker wants the verbs without an ssh hop back out.

**Four verbs, and deliberately nothing between them.** ``open`` posts one
stage open. ``wait`` polls until the wizard's session stops. ``apply`` gates a
fingerprint and posts one apply. ``bank`` files the finished session where it
outlives session retention. There is no runner, no state file and no resume:
what may follow what is the wizard's own artifact-dependency refusal to answer,
and a second sequencer here would be a weaker copy of it. ``wait`` polls
whatever session the wizard is currently publishing, so it is run directly
after ``open`` rather than against a round from yesterday.

**The apply gate is the library's, not a second opinion.** The endpoint runs
the same comparison and would refuse the same request; what
:func:`~jasper.active_speaker.wizard_client.apply_by_fingerprint` adds is that
a mistyped or stale fingerprint ends here instead of becoming a state-changing
request, with both values on the receipt.

**``bank`` reaches no wizard**: it files through
:func:`jasper.active_speaker.round_bank.bank_round` into the campaign home.

The wizard verbs print a receipt -- the fields, or the same fields as JSON
under ``--json``; ``bank`` prints the round directory. See
``docs/tuning-operator-runbook.md`` steps 6 and 8, which name this tool beside
the laptop script it shares its transport with.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from jasper.active_speaker.wizard_client import (
    CSRF_PAGE_PATH,
    REASON_ANSWER_LOST,
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
from jasper.identity import read_identity

from ._refusal import EXIT_OK, EXIT_REFUSED, EXIT_UNREADABLE, EXIT_WRITE_FAILED

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
    "terminal": EXIT_OK,
    "failed": EXIT_REFUSED,
    "lost": EXIT_UNREADABLE,
    "timed_out": EXIT_UNREADABLE,
}


def _emit(receipt: Mapping[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(dict(receipt), indent=2, sort_keys=True, default=str))
        return
    print(f"{receipt['verb']}: {receipt.get('status') or receipt.get('reason') or 'ok'}")
    for key, value in receipt.items():
        if key in ("verb", "status") or value in (None, "", {}):
            continue
        print(f"  {key:<30}{value}")


def _read_document(path: str) -> Any:
    """One prescription document, from a file or ``-`` for stdin."""
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).read_text()
        return json.loads(raw)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: {exc}") from exc


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
        key: _read_document(path)
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
        _emit(
            {"verb": "open", "status": "blocked", "stage": args.stage,
             "reason": REASON_TIER_REQUIRED},
            json_output=args.json,
        )
        return EXIT_REFUSED
    try:
        prescriptions = {} if post_apply else _prescription_doors(args)
    except ValueError as exc:
        _emit(
            {"verb": "open", "status": "blocked", "stage": args.stage,
             "reason": REASON_OPEN_REFUSED, "detail": str(exc)},
            json_output=args.json,
        )
        return EXIT_REFUSED
    http, payload = client.open_session(
        args.tier or "", stage=args.stage, prescriptions=prescriptions
    )
    block = client.v2_block() if http == 200 else {}
    _emit(
        {
            "verb": "open",
            "status": "opened" if http == 200 else "blocked",
            "stage": args.stage,
            "tier": None if post_apply else args.tier,
            "path": VERIFY_PATH if post_apply else SESSION_PATH,
            "http": http,
            "session_id": str(block.get("session_id") or ""),
            "phase": str(block.get("phase") or ""),
            "reason": (
                "" if http == 200
                else REASON_ANSWER_LOST if http == 0
                else REASON_OPEN_REFUSED
            ),
            "detail": "" if http == 200 else error_of(payload),
        },
        json_output=args.json,
    )
    if http == 200:
        return EXIT_OK
    return EXIT_UNREADABLE if http == 0 else EXIT_REFUSED


def _cmd_wait(client: WizardClient, args: argparse.Namespace) -> int:
    result = wait_for_round(
        client, timeout_s=args.timeout_s, poll_s=args.poll_s
    )
    _emit(
        {
            "verb": "wait",
            "status": result["status"],
            "reason": result["reason"],
            "phase": result["phase"],
            "session_id": result["session_id"],
            "candidate_fingerprint": result["candidate_fingerprint"],
            "failure": result["failure"],
            "waited_s": args.timeout_s if result["status"] == "timed_out" else None,
        },
        json_output=args.json,
    )
    return _EXIT_BY_WAIT_STATUS[str(result["status"])]


def _cmd_apply(client: WizardClient, args: argparse.Namespace) -> int:
    result = apply_by_fingerprint(client, args.expected_fingerprint)
    lost = result["reason"] == REASON_ANSWER_LOST
    _emit(
        {
            "verb": "apply",
            "status": result["status"],
            "reason": result["reason"],
            "refused_by": result["refused_by"],
            "expected_candidate_fingerprint":
                result["expected_candidate_fingerprint"],
            "candidate_fingerprint": result["candidate_fingerprint"],
            "http": result["http"],
            "outcome": result["outcome"],
            "detail": (
                "" if result["status"] == "applied"
                else error_of(result["payload"]) if result["payload"] is not None
                else "refused before any request left this speaker"
            ),
        },
        json_output=args.json,
    )
    if result["status"] == "applied":
        return EXIT_OK
    if lost:
        print(LOST_ANSWER_ADVICE, file=sys.stderr)
        return EXIT_UNREADABLE
    return EXIT_REFUSED


def _cmd_bank(args: argparse.Namespace) -> int:
    # Banking pulls the whole bundle and measurement import graph in: an
    # ordinary open must not pay for a door it is not carrying (ADR-0226).
    from jasper.active_speaker.round_bank import (
        DEFAULT_CAMPAIGN_ROOT,
        RoundBankError,
        bank_round,
    )

    payload: dict[str, Any]
    root = Path(args.campaign_root) if args.campaign_root else DEFAULT_CAMPAIGN_ROOT
    try:
        banked = bank_round(Path(args.session_dir), campaign_root=root)
    except RoundBankError as exc:
        payload = {"banked": False, "reason": exc.reason, "detail": str(exc)}
        code = EXIT_REFUSED
        print(f"refused ({exc.reason}): {exc}", file=sys.stderr)
    except OSError as exc:
        payload = {"banked": False, "reason": "write_failed", "detail": str(exc)}
        code = EXIT_WRITE_FAILED
        print(f"could not bank {args.session_dir}: {exc}", file=sys.stderr)
    else:
        provenance = banked.provenance
        payload = {
            "banked": True,
            "round_dir": str(banked.path),
            "provenance": provenance,
        }
        code = EXIT_OK
        if not args.json:
            print(str(banked.path))
            print(
                f"  session={provenance['session_id']} "
                f"banked_at_utc={provenance['banked_at_utc']} "
                f"installed_sha={provenance['installed_sha'] or 'unknown'} "
                f"missing={','.join(provenance['missing'] or ['none'])}",
                file=sys.stderr,
            )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return code


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
    parser.add_argument("--json", action="store_true", help="emit JSON, not text")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-round",
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
            "  /opt/jasper/.venv/bin/jasper-round open --tier express\n"
            "  /opt/jasper/.venv/bin/jasper-round wait --timeout-s 1200\n"
            "  /opt/jasper/.venv/bin/jasper-round apply "
            "--expected-fingerprint <fp>\n"
            "  /opt/jasper/.venv/bin/jasper-round bank <session-dir>\n"
            "\n"
            "WHAT THIS DOES NOT DO\n"
            "  - it does not stage an angle walk (jasper-angle-capture) or\n"
            "    run the arm (jasper-arm-walk); each is its own tool and this\n"
            "    one sequences none of them\n"
            "  - `wait` polls the session the wizard is publishing NOW, so\n"
            "    run it after `open`, not against yesterday's round\n"
            "  - `bank` files a round on this box only -- the same tree is\n"
            "    assembled over ssh by scripts/bank-crossover-round.sh -- and\n"
            "    evicts nothing: the campaign home is operator-pruned\n"
            "\n"
            "EXIT CODES\n"
            "  0  the verb did what it says -- the wizard answered, or\n"
            "     the round was banked and its directory is on stdout\n"
            "  1  EXIT_REFUSED -- the wizard's refusal, this tool's own\n"
            "     pre-flight fingerprint refusal, or a session `bank` will\n"
            "     not bank (not a bundle, unfinished, or already banked --\n"
            "     a banked round is never overwritten). Nothing was applied\n"
            "  2  EXIT_UNREADABLE -- no answer to read. The receipt's\n"
            "     `reason` says which: `answer_lost` (the daemon is down, a\n"
            "     wrong --hostname, a dropped connection) or `wait_timeout`\n"
            "     (the deadline passed with the session still running --\n"
            "     nothing was cancelled, the round is still going). A lost\n"
            "     answer to the apply POST does NOT mean the apply failed\n"
            "  3  EXIT_WRITE_FAILED -- `bank` could not write the copy: a\n"
            "     filesystem problem, not a request problem"
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
            "refused here, before anything is sent, when it is not the "
            "fingerprint the wizard is currently publishing"
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
    banker.add_argument("--json", action="store_true", help="emit JSON, not text")
    banker.set_defaults(func=_cmd_bank)
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
