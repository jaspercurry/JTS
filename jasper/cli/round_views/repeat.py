# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Repeat spread over a set of takes, or over banked rounds."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from jasper.active_speaker.crossover_v2.round_views import (
    BankedRound,
    repeat_floor_provenance,
    repeatability_spread,
)
from jasper.active_speaker.attempts_loop import percentile
from jasper.json_fields import finite_float
from jasper.active_speaker.repeat_floor import (
    DEFAULT_STATE_PATH as _REPEAT_FLOOR_DEFAULT_PATH,
    SHIPPED_POOL_METRIC,
    derive_repeat_floor, load_repeat_floor, pairwise_abs_deltas, repeat_pair,
    stopping_thresholds,
    write_repeat_floor,
)
from jasper.cli._refusal import EXIT_WRITE_FAILED, StageFailed

from ._common import (
    PROG,
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR, _load_round, _view_out, _write,
    answer, resolve_set, round_inputs, RoundViewsError, default_out,
)

def _load_rounds(round_dirs: Sequence[str]) -> list[tuple[str, BankedRound]]:
    return [(round_dir, _load_round(round_dir)) for round_dir in round_dirs]


def _cmd_repeat(args: argparse.Namespace) -> int:
    if args.set is not None:
        if len(args.round_dirs) != 1:
            raise RoundViewsError("repeat --set needs one round")
        inputs = round_inputs(Path(args.round_dirs[0]))
        selected = resolve_set(inputs, args.set)
        takes = [take for take in selected.takes if take["selected"] and
                 take["pose"].get("kind") == "bearing" and take["pose"].get("deg") == 0 and
                 take["pose"].get("elevation_deg") == 0]
        if len(takes) < 2:
            raise RoundViewsError("repeat needs two selected takes at the mark")
        if any(take["pose"] != takes[0]["pose"] for take in takes):
            raise RoundViewsError("repeat needs one fixed microphone pose")
        roles: dict[str, dict[str, list[float]]] = {}
        for take in takes:
            analysis = take.get("analysis")
            if not isinstance(analysis, dict) or not isinstance(analysis.get("trim_db"), dict):
                raise RoundViewsError("take analysis is unavailable")
            for role, trim in analysis["trim_db"].items():
                polarity = {"normal": 1.0, "inverted": -1.0}.get(str(analysis.get("polarity")))
                values = {"delay_us": analysis.get("delay_us"), "polarity": polarity,
                          "trim_db": trim, "ripple_db": analysis.get("predicted_ripple_db")}
                for metric, value in values.items():
                    number = finite_float(value)
                    if number is None:
                        raise RoundViewsError(f"take analysis lacks {role}.{metric}")
                    roles.setdefault(role, {}).setdefault(metric, []).append(number)
        if any(len(values) != len(takes) for metrics in roles.values() for values in metrics.values()) or not roles:
            raise RoundViewsError("takes have different driver roles")
        samples = {f"{role}_{metric}": values for role, metrics in roles.items() for metric, values in metrics.items()}
        units = {f"{role}_{metric}": "us" if metric == "delay_us" else "sign" if metric == "polarity" else "db"
                 for role, metrics in roles.items() for metric in metrics}
        floor = derive_repeat_floor(samples=samples, units=units, rounds=[{"take_id": take["take_id"]} for take in takes])
        reference = load_repeat_floor(state_path=inputs.repeat_floor_path) if inputs.repeat_floor_path else None
        payload = {"set_id": selected.set_id, "take_ids": [take["take_id"] for take in takes],
                   "roles": {role: {metric: {"values": values, "median": percentile(values, 50),
                             "spread": percentile(pairwise_abs_deltas(values), 95), "n": len(values)}
                             for metric, values in metrics.items()} for role, metrics in roles.items()},
                   "floor": floor, "pair_take_ids": [take["take_id"] for take in takes[:2]],
                   "pair_floor": str(inputs.repeat_floor_path) if reference else None, **repeat_pair(roles, reference)}
        written = _write(payload, args.out, default_out(inputs, Path(args.round_dirs[0]), "repeat.json", selected.set_id))
        return answer(args.command, out=written, line=f"repeat: {len(takes)} takes; {payload['pair']}", **payload)
    rounds = _load_rounds(args.round_dirs)
    result = repeatability_spread(rounds)
    written = _write(result.to_dict(), args.out, _view_out(args, rounds[0][1]))
    shipped = next((m for m in result.metrics if m.name == SHIPPED_POOL_METRIC), None)
    return answer(args.command, out=written, rounds=len(result.round_labels), metric=SHIPPED_POOL_METRIC,
                  spread=shipped.spread() if shipped else None, line=f"repeat: {len(rounds)} rounds")


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
    repeat = sub.add_parser("repeat", help="per-take spread at the mark with --set; otherwise compare rounds")
    repeat.add_argument("round_dirs", nargs="+", metavar=_ROUND_DIR_METAVAR,
        help=f"one with --set, otherwise two or more: {_ROUND_DIR_HELP}",
    )
    repeat.add_argument("--set", help="manifest set of repeated Speaker takes")
    repeat.add_argument("--out", default=None, help="write the result here")
    repeat.set_defaults(func=_cmd_repeat)

    repeat_floor = sub.add_parser("repeat-floor", help="bank the repeat spread as the floor the evidence packet reads")
    repeat_floor.add_argument("round_dirs", nargs="+", metavar=_ROUND_DIR_METAVAR,
        help="two or more TOUCHED-NOTHING fixed-pose repeat round directories",
    )
    repeat_floor.add_argument("--install", action="store_true",
                              help=f"publish at {_REPEAT_FLOOR_DEFAULT_PATH}; needs sudo")
    repeat_floor.add_argument("--out", default=None, type=Path,
        help="write the floor here; requires --install, --out, or both",
    )
    repeat_floor.set_defaults(func=_cmd_repeat_floor, parser=repeat_floor)
