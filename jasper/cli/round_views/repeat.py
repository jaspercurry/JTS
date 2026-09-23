# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Mark-take repeatability: one set's repeated takes, or each driver's mark
takes within and between rounds, on the fit's own statistic (ADR-0341)."""

from __future__ import annotations

import argparse
from itertools import combinations, product
from pathlib import Path
from typing import Any, Mapping, Sequence

from jasper.active_speaker.crossover_v2.position_cycle import measured_curve_band
from jasper.active_speaker.crossover_v2.round_inputs import SetTakes
from jasper.active_speaker.crossover_v2.round_views import (
    BankedRound,
    repeat_floor_provenance,
    repeatability_spread,
)
from jasper.active_speaker.attempts_loop import percentile
from jasper.active_speaker.round_verdicts import held_pairs, mark_takes, pair_spread
from jasper.active_speaker.run_manifest import view_sets
from jasper.json_fields import finite_float
from jasper.active_speaker.repeat_floor import (
    DEFAULT_STATE_PATH as _REPEAT_FLOOR_DEFAULT_PATH,
    SHIPPED_POOL_METRIC,
    derive_repeat_floor, pairwise_abs_deltas,
    stopping_thresholds,
    write_repeat_floor,
)
from jasper.cli._refusal import EXIT_UNREADABLE, EXIT_WRITE_FAILED, StageFailed, stage

from ._common import (
    ARTIFACT_BY_VIEW,
    PROG,
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR, _ROUND_TOOL_ERRORS, _load_round, _write,
    answer, read_run_manifest, resolve_set, round_inputs, RoundSetRefused, RoundViewsError, default_out,
)

#: No driver has mark takes in two of the named rounds, so nothing compares.
REFUSE_NO_SHARED_MARK_SET = "repeat_no_shared_mark_set"


def _load_rounds(round_dirs: Sequence[str]) -> list[tuple[str, BankedRound]]:
    return [(round_dir, _load_round(round_dir)) for round_dir in round_dirs]


def _common_band(takes: Sequence[Mapping[str, Any]]) -> list[float] | None:
    """The band every take measured, so each spread speaks for one span."""
    bands = [measured[2] for take in takes if (measured := measured_curve_band(take.get("curve") or {}))]
    return [max(lo for lo, _ in bands), min(hi for _, hi in bands)] if bands else None


def _cmd_repeat_set(args: argparse.Namespace) -> int:
    if len(args.round_dirs) != 1:
        raise RoundViewsError("repeat --set needs one round")
    inputs = round_inputs(Path(args.round_dirs[0]))
    selected = resolve_set(inputs, args.set)
    takes = selected.on_axis
    if len(takes) < 2:
        raise RoundViewsError("repeat needs two selected takes at the mark")
    if any(take["pose"] != takes[0]["pose"] for take in takes):
        raise RoundViewsError("repeat needs one fixed microphone pose")
    take_values: dict[str, list[float]] = {}
    trims: dict[str, list[float]] = {}
    for take in takes:
        analysis = take.get("analysis")
        if not isinstance(analysis, dict) or not isinstance(analysis.get("trim_db"), dict):
            raise RoundViewsError("take analysis is unavailable")
        polarity = {"normal": 1.0, "inverted": -1.0}.get(str(analysis.get("polarity")))
        for target, values in ((take_values, {"delay_us": analysis.get("delay_us"), "polarity": polarity,
                                              "ripple_db": analysis.get("predicted_ripple_db")}),
                               (trims, analysis["trim_db"])):
            for metric, value in values.items():
                number = finite_float(value)
                if number is None:
                    raise RoundViewsError(f"take analysis lacks {metric}")
                target.setdefault(metric, []).append(number)
    if not trims or any(len(values) != len(takes) for values in trims.values()):
        raise RoundViewsError("takes have different driver roles")
    samples = {**take_values, **{f"{role}_trim_db": values for role, values in trims.items()}}
    floor = derive_repeat_floor(samples=samples, units={"delay_us": "us", "polarity": "sign"},
                                rounds=[{"take_id": take["take_id"]} for take in takes])
    summaries = {metric: {"values": values, "median": percentile(values, 50),
                 "spread": percentile(pairwise_abs_deltas(values), 95.0), "n": len(values)}
                 for metric, values in samples.items()}
    held = mark_takes(selected, selected.capture_basis.get("role"))
    band = _common_band(held)
    marks = {"band_hz": band, **pair_spread(held_pairs(held), band)}
    payload = {"set_id": selected.set_id, "take_ids": [take["take_id"] for take in takes],
               "take": {metric: summaries[metric] for metric in take_values},
               "roles": {role: {"trim_db": summaries[f"{role}_trim_db"]} for role in trims},
               "floor": floor, "mark_pairs": marks}
    written = _write(payload, args.out, default_out(inputs, Path(args.round_dirs[0]), "repeat.json", selected.set_id))
    return answer(args.command, out=written, line=f"repeat: {len(takes)} takes; mark spread {_db(marks)}", **payload)


def _db(spread: Mapping[str, Any]) -> str:
    value = spread["repeat_spread_db"]
    return f"{value:.2f} dB" if value is not None else str(spread["reason"])


def _cmd_repeat_rounds(args: argparse.Namespace) -> int:
    if len(args.round_dirs) < 2:
        raise RoundViewsError("repeat needs --set with one round, or two or more rounds")
    rounds = [(Path(path), stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, Path(path)))
              for path in args.round_dirs]
    marks: dict[tuple[Any, Any], list[tuple[int, str, list[Mapping[str, Any]]]]] = {}
    for index, (_path, inputs) in enumerate(rounds):
        for row in view_sets(read_run_manifest(inputs)):
            basis = row["capture_basis"]
            takes = mark_takes(SetTakes.from_row(row), basis.get("role"))
            if takes:
                marks.setdefault((basis.get("side"), basis.get("role")), []).append((index, row["set_id"], takes))
    drivers = []
    for (side, role), sets in marks.items():
        band = _common_band([take for *_, takes in sets for take in takes])
        drivers.append({
            "side": side, "role": role, "band_hz": band,
            "within": [{"round": index, "set_id": set_id, "take_ids": [take["take_id"] for take in takes],
                        **pair_spread(held_pairs(takes), band)} for index, set_id, takes in sets],
            "between": [{"rounds": [a[0], b[0]], "set_ids": [a[1], b[1]],
                         **pair_spread(list(product(a[2], b[2])), band)}
                        for a, b in combinations(sets, 2) if a[0] != b[0]],
        })
    names = [str(path) for path, _ in rounds]
    if not any(driver["between"] for driver in drivers):
        raise RoundSetRefused(REFUSE_NO_SHARED_MARK_SET, rounds=names, drivers=[
            {"side": driver["side"], "role": driver["role"], "rounds": [row["round"] for row in driver["within"]]}
            for driver in drivers])
    written = _write({"rounds": names, "drivers": drivers}, args.out,
                     default_out(rounds[0][1], rounds[0][0], ARTIFACT_BY_VIEW[args.command].artifact))
    keys = ("repeat_spread_db", "n_pairs", "reason")
    return answer(
        args.command, out=written, rounds=names, drivers=[
            {"side": driver["side"], "role": driver["role"], "band_hz": driver["band_hz"],
             "within": [{"round": row["round"], **{key: row[key] for key in keys}} for row in driver["within"]],
             "between": [{"rounds": row["rounds"], **{key: row[key] for key in keys}} for row in driver["between"]]}
            for driver in drivers],
        line=f"repeat: {len(names)} rounds; " + "; ".join(
            f"{driver['role']} within {', '.join(_db(row) for row in driver['within'])}, "
            f"between {', '.join(_db(row) for row in driver['between']) or 'none'}" for driver in drivers),
    )


def _cmd_repeat(args: argparse.Namespace) -> int:
    return _cmd_repeat_set(args) if args.set is not None else _cmd_repeat_rounds(args)


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
    repeat = sub.add_parser(
        "repeat", help="mark-take spread: one set with --set, otherwise each driver within and between rounds",
    )
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
