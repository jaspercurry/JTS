# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Serve the microphone arm against the daemon's position gate."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Sequence

from jasper.active_speaker import arm_walk
from ._logging import CLI_LOG_FORMAT
from ._refusal import EXIT_OK, EXIT_REFUSED, failed

MOVER_TURNTABLE = "turntable"
AUTHORITY_TIER = "mutating (`serve` moves the arm)"

def _cmd_serve(args: argparse.Namespace) -> int:
    """Run the arm through one live session and hand back a shared exit code.

    The loop owns a stall vocabulary of its own
    (``jasper.active_speaker.arm_walk.EXIT_NAMES``); it is published here as the
    refusal's ``reason`` rather than as a number, because a tool in the menu
    exits 0/1/2/3 and nothing else (docs/tuning-operator-runbook.md, "Exit
    codes"). A park signal leaves through ``install_park_on_signals``' own
    ``128 + signum`` instead, which is the shell's spelling and not this
    module's to assign.
    """
    try:
        config = arm_walk.WalkConfig(
            settle_s=args.settle_s,
            poll_s=args.poll_s,
            idle_ceiling_s=args.idle_ceiling_s,
            stuck_alarm_s=args.stuck_alarm_s,
        )
    except arm_walk.ArmWalkRefused as exc:
        return failed(EXIT_REFUSED, "walk_refused", str(exc))

    arm_walk.install_park_on_signals()
    trail = arm_walk.Trail(args.trail)
    walk = arm_walk.ArmWalk(
        arm_walk.TurntableMover(
            tool_path=args.tool, attest_rig_clear=args.attest_rig_clear
        ),
        arm_walk.LoopbackSession(host_header=args.hostname, base_url=args.base_url),
        config,
        trail=trail,
    )
    try:
        code = walk.run()
    finally:
        # In a ``finally`` because a park signal leaves through here with no
        # return value at all, and the summary is then the only account of what
        # the walk served before it was stopped.
        print(walk.summary(), file=sys.stderr)
        trail.close()
    if code != arm_walk.EXIT_OK:
        return failed(
            EXIT_REFUSED,
            arm_walk.EXIT_NAMES.get(code, str(code)),
            # The park runs in the walk's own unwind and NEVER raises, so no
            # sentence here may claim the arm came home: the `parked` trail row
            # is the only place that is answered.
            f"the {args.mover} walk stopped at loop code {code}; its 'parked' "
            "trail row is where the arm's return is confirmed",
        )
    print(
        f"{args.mover} walk finished: ok; envelope "
        f"+/-{arm_walk.ARM_ENVELOPE_DEG} deg",
        file=sys.stderr,
    )
    return EXIT_OK


def _add_serve_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--attest-rig-clear",
        action="store_true",
        required=True,
        help=(
            "attest, once for this run, that the arm's full travel path is "
            "clear and the saved zero is the acoustic axis. Maps to the "
            "turntable adapter's two --confirm-* flags on every move. A power "
            "sign voids it: the walk then stops, parks, and refuses"
        ),
    )
    parser.add_argument(
        "--hostname",
        required=True,
        help=(
            "the speaker's own hostname (JASPER_HOSTNAME, e.g. jts3.local). "
            "Sent as the Host header so the wizard's management-host guard "
            "admits a loopback request"
        ),
    )
    parser.add_argument(
        "--mover",
        default=MOVER_TURNTABLE,
        choices=(MOVER_TURNTABLE,),
        help=(
            "which rig serves the gate (default: %(default)s). Not "
            "the plan's mover, which states who moves the microphone"
        ),
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1",
        help="where the wizard is reached (default: %(default)s)",
    )
    parser.add_argument(
        "--tool",
        type=Path,
        default=arm_walk.DEFAULT_TOOL_PATH,
        help=(
            "the turntable adapter to drive as a subprocess "
            "(default: %(default)s; point it at a checkout for lab work)"
        ),
    )
    parser.add_argument(
        "--settle-s",
        type=float,
        default=arm_walk.DEFAULT_SETTLE_S,
        help=(
            f"settle after each move before reporting the microphone in place "
            f"(default: %(default)s; refused under the "
            f"{arm_walk.SETTLE_FLOOR_S:.0f}s floor a landed arm needs)"
        ),
    )
    parser.add_argument(
        "--poll-s", type=float, default=arm_walk.DEFAULT_POLL_S,
        help="how often to read the envelope (default: %(default)s)",
    )
    parser.add_argument(
        "--idle-ceiling-s", type=float, default=arm_walk.DEFAULT_IDLE_CEILING_S,
        help="give up when nothing is pending this long (default: %(default)s)",
    )
    parser.add_argument(
        "--stuck-alarm-s", type=float, default=arm_walk.DEFAULT_STUCK_ALARM_S,
        help=(
            "in flight, nothing pending, nothing released this long is a "
            "capture awaiting a human -- name it and stop (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--trail", type=Path, default=None,
        help="append one JSON object per event to this file",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jasper-angle-capture", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="serve the arm against a live position gate")
    _add_serve_args(serve)
    serve.set_defaults(func=_cmd_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format=CLI_LOG_FORMAT)
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
