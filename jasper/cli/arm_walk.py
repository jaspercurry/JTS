# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Run one lab-arm walk against a live crossover-v2 measurement session.

The operator's door onto :mod:`jasper.active_speaker.arm_walk` -- read that
module for the loop, the safety invariants and the exit-code meanings; this file
is argument parsing, the two seam constructions, and the signal handlers that
make SIGTERM take the same parking exit a clean finish takes.

Runs ON the speaker, in the foreground, one run per walk::

    sudo -u jasper /opt/jasper/.venv/bin/jasper-arm-walk \\
        --attest-rig-clear --expect-angles 7,-7,22,-22 \\
        --complete-after 5 --trail /tmp/walk.jsonl

Start it BEFORE opening the measurement session: the first poll is what tells it
whether a staged walk is still waiting, which is the one check it can make before
anything moves.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path
from typing import Sequence

from jasper.active_speaker.arm_walk import (
    ARM_ENVELOPE_DEG,
    DEFAULT_IDLE_CEILING_S,
    DEFAULT_POLL_S,
    DEFAULT_SETTLE_S,
    DEFAULT_STUCK_ALARM_S,
    DEFAULT_TOOL_PATH,
    EXIT_NAMES,
    EXIT_REFUSED,
    SETTLE_FLOOR_S,
    ArmWalk,
    ArmWalkRefused,
    LoopbackSession,
    Trail,
    TurntableMover,
    WalkConfig,
    staged_walk_pending,
)

from ._logging import CLI_LOG_FORMAT


def _angles(raw: str) -> tuple[int, ...]:
    """``"7,-7,22"`` -> ``(7, -7, 22)``. Whole degrees only, never rounded.

    The same rule the angle seam holds and for the same reason: ``int("7.5")``
    raises but ``int(7.5)`` is ``7``, and a silently truncated angle is a pose
    nobody asked for.
    """
    fields = [field.strip() for field in raw.split(",") if field.strip()]
    try:
        return tuple(int(field) for field in fields)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "angles are whole degrees, comma separated (e.g. 7,-7,22,-22)"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-arm-walk",
        description=(
            "Serve a crossover-v2 measurement session's position gate with the "
            "lab turntable arm: poll, move, settle, report the microphone in "
            "place. Parks the arm at 0 deg on every exit."
        ),
    )
    parser.add_argument(
        "--attest-rig-clear",
        action="store_true",
        required=True,
        help=(
            "attest, once for this run, that the arm's full travel path is "
            "clear and the saved zero is the acoustic axis. Maps to the "
            "turntable adapter's two --confirm-* flags on every move. A power "
            "sign voids it: the walk then stops, parks, and exits non-zero"
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
        "--base-url",
        default="http://127.0.0.1",
        help="where the wizard is reached (default: %(default)s)",
    )
    parser.add_argument(
        "--tool",
        type=Path,
        default=DEFAULT_TOOL_PATH,
        help=(
            "the turntable adapter to drive as a subprocess "
            "(default: %(default)s; point it at a checkout for lab work)"
        ),
    )
    parser.add_argument(
        "--expect-angles",
        type=_angles,
        default=(),
        help=(
            "the non-zero angles the staged walk contributes. Given, the run "
            "refuses to start when no walk is staged and no session is open, "
            "and exits non-zero if any stated angle never became a pending -- "
            "which is how a walk the session refused (and silently replaced "
            "with its ordinary shape) is caught instead of measured"
        ),
    )
    parser.add_argument(
        "--complete-after",
        type=int,
        default=None,
        help=(
            "after this many releases, POST the wired all-spots-measured "
            "signal that closes the held pre-apply group. A wired stage has no "
            "phone event to close it, so nothing else will"
        ),
    )
    parser.add_argument(
        "--settle-s",
        type=float,
        default=DEFAULT_SETTLE_S,
        help=(
            f"settle after each move before reporting the microphone in place "
            f"(default: %(default)s; refused under the {SETTLE_FLOOR_S:.0f}s "
            f"floor a landed arm needs)"
        ),
    )
    parser.add_argument(
        "--poll-s", type=float, default=DEFAULT_POLL_S,
        help="how often to read the envelope (default: %(default)s)",
    )
    parser.add_argument(
        "--idle-ceiling-s", type=float, default=DEFAULT_IDLE_CEILING_S,
        help="give up when nothing is pending this long (default: %(default)s)",
    )
    parser.add_argument(
        "--stuck-alarm-s", type=float, default=DEFAULT_STUCK_ALARM_S,
        help=(
            "in flight, nothing pending, nothing released this long is a "
            "capture awaiting a human -- name it and exit (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--trail", type=Path, default=None,
        help="append one JSON object per event to this file",
    )
    return parser


def _install_park_on_signals() -> None:
    """SIGTERM/SIGINT become ``SystemExit``, so the run's park still happens.

    :meth:`~jasper.active_speaker.arm_walk.ArmWalk.run` parks in a ``finally``
    that a bare default SIGTERM would skip entirely -- the arm would be left
    wherever the walk stopped, which is the one state the rig must never be
    abandoned in.
    """
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format=CLI_LOG_FORMAT)
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        config = WalkConfig(
            settle_s=args.settle_s,
            poll_s=args.poll_s,
            idle_ceiling_s=args.idle_ceiling_s,
            stuck_alarm_s=args.stuck_alarm_s,
            complete_after=args.complete_after,
            expect_angles=tuple(args.expect_angles),
        )
    except ArmWalkRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    _install_park_on_signals()
    trail = Trail(args.trail)
    walk = ArmWalk(
        TurntableMover(tool_path=args.tool, attest_rig_clear=True),
        LoopbackSession(host_header=args.hostname, base_url=args.base_url),
        config,
        trail=trail,
        walk_staged=staged_walk_pending,
    )
    try:
        code = walk.run()
    finally:
        print(walk.summary(), file=sys.stderr)
        trail.close()
    print(
        f"arm walk finished: {EXIT_NAMES.get(code, str(code))} (rc {code}); "
        f"envelope +/-{ARM_ENVELOPE_DEG} deg",
        file=sys.stderr,
    )
    return code


if __name__ == "__main__":  # pragma: no cover - console-script entry point
    raise SystemExit(main())
