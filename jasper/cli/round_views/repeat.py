# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Session-to-session repeatability: the spread of the pooled honest
figures, and the durable floor the evidence packet reads.

* ``repeat <round-dir> [<round-dir> ...]`` — session-to-session spread of
  the pooled honest figures (the stop criterion). Writes
  ``repeatability.json`` for the FIRST round.
* ``repeat-floor <round-dir> <round-dir> [...] (--install | --out PATH)`` —
  the same spread, banked as the durable record the evidence packet's
  ``in_capture_repeat_floor`` reads and derives the stopping plateau/benefit
  margin from. The rounds must be touched-nothing fixed-pose repeats.
  ``--install`` publishes it at the on-speaker path, from which
  ``bank-crossover-round.sh`` pulls it beside every later round as
  ``repeat-floor.json``; ``--out`` writes the same record somewhere else
  (beside a banked round, say). At least one is required: running on a laptop
  over banked directories, the speaker's path is a destination to ask for,
  never one to assume.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from jasper.active_speaker.crossover_v2.round_views import (
    BankedRound,
    repeat_floor_provenance,
    repeatability_spread,
)
from jasper.active_speaker.repeat_floor import (
    DEFAULT_STATE_PATH as _REPEAT_FLOOR_DEFAULT_PATH,
    SHIPPED_POOL_METRIC,
    derive_repeat_floor,
    stopping_thresholds,
    write_repeat_floor,
)
from jasper.cli._refusal import EXIT_WRITE_FAILED, StageFailed

from ._common import (
    PROG,
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _load_round,
    _view_out,
    _write,
    answer,
)

def _load_rounds(round_dirs: Sequence[str]) -> list[tuple[str, BankedRound]]:
    """The (label, round) pairs both repeat verbs grade, labelled by the
    directory the operator named."""
    return [(round_dir, _load_round(round_dir)) for round_dir in round_dirs]


def _cmd_repeat(args: argparse.Namespace) -> int:
    rounds = _load_rounds(args.round_dirs)
    result = repeatability_spread(rounds)
    written = _write(result.to_dict(), args.out, _view_out(args, rounds[0][1]))
    shipped = next((m for m in result.metrics if m.name == SHIPPED_POOL_METRIC), None)
    spread = shipped.spread() if shipped else None
    return answer(
        args.command, out=written, rounds=len(result.round_labels),
        metric=SHIPPED_POOL_METRIC, spread=spread,
        line=(
            f"repeatability: {len(result.round_labels)} round(s); "
            f"{SHIPPED_POOL_METRIC} spread={spread}"
            f"{f' -> {written}' if written else ''}"
        ),
    )


def _record_path(value: str) -> Path:
    """``--out`` for a verb that publishes a file: ``-`` is not a path."""
    if value == "-":
        raise argparse.ArgumentTypeError("repeat-floor publishes a file; '-' is not a path")
    return Path(value)


#: ``--install``'s destination is a 0770 StateDirectory owned by the daemon's
#: user, so the login account cannot write it unaided.
_INSTALL_SUDO_HINT = f" — run it with sudo -n /opt/jasper/.venv/bin/{PROG} ..."


def _cmd_repeat_floor(args: argparse.Namespace) -> int:
    destinations: list[tuple[Path, str]] = []
    if args.out is not None:
        destinations.append((args.out, ""))
    if args.install:
        destinations.append((_REPEAT_FLOOR_DEFAULT_PATH, _INSTALL_SUDO_HINT))
    if not destinations:
        args.parser.error("nowhere to publish: pass --install, --out PATH, or both")
    rounds = _load_rounds(args.round_dirs)
    payload = derive_repeat_floor(
        repeatability_spread(rounds),
        rounds=[repeat_floor_provenance(round_dir, banked) for round_dir, banked in rounds],
    )
    for path, hint in destinations:
        try:
            write_repeat_floor(payload, state_path=path)
        except OSError as exc:  # an unwritable destination is the WRITE exit
            detail = f"{path}: {exc}"
            raise StageFailed(
                EXIT_WRITE_FAILED,
                OSError(detail + hint if isinstance(exc, PermissionError) else detail),
            ) from exc
    thresholds = stopping_thresholds(payload)
    aggregate = payload["metrics"][SHIPPED_POOL_METRIC]
    return answer(
        # The install path when it was asked for: that is the one
        # bank-crossover-round.sh pulls beside every later round.
        args.command, out=destinations[-1][0], n_repeats=payload["n_repeats"],
        metric=SHIPPED_POOL_METRIC, sd_db=aggregate["sd_db"],
        thresholds=thresholds, installed=args.install,
        line=(
            f"repeat-floor: {payload['n_repeats']} round(s); {SHIPPED_POOL_METRIC} "
            f"sd={aggregate['sd_db']:.4g} dB; thresholds={thresholds} -> "
            + ", ".join(str(path) for path, _ in destinations)
        ),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    repeat = sub.add_parser("repeat", help="session-to-session spread of the pooled honest figures")
    repeat.add_argument(
        "round_dirs", nargs="+", metavar=_ROUND_DIR_METAVAR,
        help=f"two or more of: {_ROUND_DIR_HELP}",
    )
    repeat.add_argument("--out", default=None, help="write the result here (- for stdout)")
    repeat.set_defaults(func=_cmd_repeat)

    repeat_floor = sub.add_parser(
        "repeat-floor", help="bank the repeat spread as the floor the evidence packet reads",
    )
    repeat_floor.add_argument(
        "round_dirs", nargs="+", metavar=_ROUND_DIR_METAVAR,
        help="two or more TOUCHED-NOTHING fixed-pose repeat round directories",
    )
    repeat_floor.add_argument(
        "--install", action="store_true",
        help=(
            f"publish it on the speaker at {_REPEAT_FLOOR_DEFAULT_PATH}, from "
            "which bank-crossover-round.sh pulls it beside every later round; "
            "needs sudo"
        ),
    )
    repeat_floor.add_argument(
        "--out", default=None, type=_record_path,
        help=(
            "also write the record here, e.g. beside a banked round as "
            "repeat-floor.json; at least one of --install/--out is required"
        ),
    )
    repeat_floor.set_defaults(func=_cmd_repeat_floor, parser=repeat_floor)
