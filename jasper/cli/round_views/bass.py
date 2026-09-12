# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Bass views."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused, refusal_copy_for
from jasper.cli._refusal import EXIT_REFUSED, EXIT_UNREADABLE, failed

from ._common import ARTIFACT_BY_VIEW, REASON_UNREADABLE, RoundSetRefused, _ROUND_TOOL_ERRORS, _write, add_set_argument, answer, default_out, read_run_manifest, resolve_set, round_inputs


def add_parser(sub: argparse._SubParsersAction) -> None:
    for name, help_text in (("bass", "bass response, quiet-window SNR and H2/H3"),
                            ("bass-compare", "compare selected bass sets"),
                            ("bass-fit-table", "fit all candidate/level pairs in a run")):
        parser = sub.add_parser(name, help=help_text)
        parser.add_argument("--out")
        parser.set_defaults(func=_cmd)
        if name == "bass-compare":
            parser.add_argument("before", type=Path)
            parser.add_argument("after", type=Path)
            add_set_argument(parser, name="--before-set", take=True)
            add_set_argument(parser, name="--after-set", take=True)
            parser.add_argument("--change", required=True, choices=("candidate", "volume", "demand", "diagnostic"))
            continue
        parser.add_argument("round_dir", type=Path)
        if name == "bass":
            add_set_argument(parser)
            parser.add_argument("--calibration-root", type=Path)
        else:
            parser.add_argument("--run", required=True, help="run ID recorded in this round's manifest")
            parser.add_argument("--candidate", type=Path, action="append", required=True, help="measured candidate artifact; repeat for each candidate")
            parser.add_argument("--target", type=Path, required=True, help="target curve JSON: freqs_hz, magnitude_db")
            parser.add_argument("--tolerance-db", type=float, required=True)
            parser.add_argument("--reference-band-hz", type=float, nargs=2)


def _compare(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    from jasper.active_speaker.bass_comparison import compare_bass_takes, selected_take  # lazy: laptop array analysis

    manifests, paths, takes = {}, [], []
    for root, set_id, take_id in ((args.before, args.before_set, args.before_take),
                                  (args.after, args.after_set, args.after_take)):
        inputs = round_inputs(root)
        key = inputs.session_dir.resolve()
        if key not in manifests:
            manifests[key] = read_run_manifest(inputs)
        selected = resolve_set(inputs, set_id, manifest=manifests[key])
        selected_id = selected.take_id(take_id)
        path = default_out(inputs, root, ARTIFACT_BY_VIEW["bass"].artifact, set_id)
        takes.append(selected_take(json.loads(path.read_text()), selected_id))
        paths.append(str(path))
    return ({**compare_bass_takes(*takes, change=args.change), "source_views": paths},
            default_out(inputs, args.after, ARTIFACT_BY_VIEW[args.command].artifact, args.after_set))


def _cmd(args: argparse.Namespace) -> int:
    try:
        if args.command == "bass-compare":
            payload, destination = _compare(args)
            summary: dict[str, Any] = {key: payload[key] for key in ("available", "context", "bands")}
        else:
            inputs = round_inputs(args.round_dir)
            destination = default_out(inputs, args.round_dir, ARTIFACT_BY_VIEW[args.command].artifact,
                                      args.set if args.command == "bass" else None)
            if args.command == "bass":
                from jasper.active_speaker.measurement_bass import bass_view  # lazy: laptop FFT analysis
                selected = resolve_set(inputs, args.set)
                payload = bass_view(inputs.session_dir, calibration_root=args.calibration_root)
                payload["takes"] = [take for take in payload["takes"] if take["record"]["take_id"] in selected.selected_ids]
                summary = {"takes": len(payload["takes"])}
            else:
                from jasper.active_speaker.bass_fit import REFERENCE_BAND_HZ  # lazy: laptop array analysis
                from ._bass_inputs import fit_run  # lazy: laptop array analysis
                args.reference_band_hz = args.reference_band_hz or REFERENCE_BAND_HZ
                payload = fit_run(inputs, args)
                levels = [row for table in payload["tables"] for row in table["levels"]]
                summary = {"run_id": payload["run_id"], "level_count": len(levels),
                           "outcomes": [row["outcome"] for row in levels]}
    except CrossoverV2Refused as refusal:
        message, action = refusal_copy_for(refusal.code)
        return failed(EXIT_REFUSED, refusal.code, refusal.args[0] if refusal.args else message,
                      code=refusal.code, next_action=action)
    except RoundSetRefused:
        raise
    except _ROUND_TOOL_ERRORS as exc:
        return failed(EXIT_UNREADABLE, REASON_UNREADABLE, str(exc))
    written = _write(payload, args.out, destination)
    return answer(args.command, out=written, **summary, line=f"{args.command} -> {written}")
