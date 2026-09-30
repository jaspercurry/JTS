# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Bass views."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jasper.active_speaker.bass_table_report import bass_table_markdown, bass_table_rows
from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused, refusal_copy_for
from jasper.active_speaker.crossover_v2.round_inputs import comparand
from jasper.active_speaker.crossover_v2.take_reading import (
    REFUSE_BASS_COMPARAND_VIEW_NOT_FILED, REFUSE_COMPARE_NO_COMPARAND,
)
from jasper.active_speaker.round_view_builders import bass_payload
from jasper.audio_measurement.band_ladders import BASS_FIT_REFERENCE_BAND_HZ
from jasper.cli._refusal import EXIT_REFUSED, EXIT_UNREADABLE, failed

from ._common import (
    ARTIFACT_BY_VIEW, REASON_UNREADABLE, RoundSetRefused, _ROUND_DIR_HELP, _ROUND_DIR_METAVAR, _ROUND_TOOL_ERRORS,
    _write, add_set_argument, answer, calibration_id, default_out, read_run_manifest, resolve_set, resolved_out,
    round_inputs, subject,
)


def add_parser(sub: argparse._SubParsersAction) -> None:
    for name, help_text in (("bass", "bass response, quiet-window SNR and H2/H3"),
                            ("bass-compare", "compare selected bass sets"),
                            ("bass-fit-table", "read candidate reach, drive and headroom by level")):
        parser = sub.add_parser(name, help=help_text)
        parser.add_argument("--out", help="artifact destination")
        parser.set_defaults(func=_cmd)
        if name == "bass-compare":
            parser.add_argument("before", type=Path, metavar="<before-round>",
                                help=f"the round before the change: {_ROUND_DIR_HELP}. Named alone with no --before-* "
                                     "flag, it is the after take's round, and the before take is that take's comparand "
                                     "(ADR-0391): the round's base take at its place, else the newest earlier banked "
                                     "take at the same place, side, role and graph scope")
            parser.add_argument("after", type=Path, nargs="?", metavar="<after-round>",
                                help="the round after it; default: the before round")
            add_set_argument(parser, name="--before-set", take=True)
            add_set_argument(parser, name="--after-set", take=True)
            parser.add_argument("--change", required=True, choices=("candidate", "volume", "demand", "diagnostic"),
                                help="what changed between the two sets")
            continue
        if name == "bass":
            parser.add_argument("round_dir", type=Path, metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
            add_set_argument(parser)
        else:
            parser.add_argument("round_dir", type=Path, nargs="+", metavar=_ROUND_DIR_METAVAR,
                                help=f"each bass round to read: {_ROUND_DIR_HELP}")
            parser.add_argument("--candidate", type=Path, action="append", required=True, help="candidate artifact or banked fingerprint; repeat for each candidate")
            parser.add_argument("--reference-band-hz", type=float, nargs=2, metavar=("LOW", "HIGH"),
                                default=list(BASS_FIT_REFERENCE_BAND_HZ),
                                help="band where each candidate take is level-matched to its baseline, in Hz "
                                     f"(default: {' '.join(f'{hz:g}' for hz in BASS_FIT_REFERENCE_BAND_HZ)})")


def _compare(args: argparse.Namespace) -> tuple[dict[str, Any], Path, list[dict[str, Any]]]:
    from jasper.active_speaker.bass_comparison import compare_bass_takes, selected_take  # lazy: laptop array analysis
    from jasper.active_speaker.bass_table_inputs import bass_view_path  # lazy: laptop array analysis

    manifests: dict[Path, Any] = {}

    def side(root: Path, set_id: str | None, take_id: str | None, source: str | None = None) -> tuple[Any, ...]:
        """One side's bass take, the view it is read from, what it read, its set and its take id.
        A comparand (``source``) whose round filed no bass view refuses by name: the
        rule matches place, drivers and graph scope, not the program (ADR-0391 §1)."""
        inputs = round_inputs(root)
        key = inputs.session_dir.resolve()
        if key not in manifests:
            manifests[key] = read_run_manifest(inputs)
        selected = resolve_set(inputs, set_id, manifest=manifests[key]).with_records(
            inputs.session_dir, every_take=take_id is not None)
        take_id = selected.take_id(take_id)
        path = bass_view_path(inputs, root, selected.set_id, manifests[key])
        read = subject(inputs, selected, take_ids=[take_id])
        if source is not None and not path.is_file():
            raise CrossoverV2Refused({"round_id": read.get("round_id"), "set_id": selected.set_id,
                                      "take_id": take_id, "source": source}, code=REFUSE_BASS_COMPARAND_VIEW_NOT_FILED)
        return selected_take(json.loads(path.read_text()), take_id), str(path), read, selected, take_id

    after_round = args.after or args.before
    source = None
    if args.after is None and args.before_set is None and args.before_take is None:
        after = side(after_round, args.after_set, args.after_take)
        group, take_id = after[3:]
        found = comparand(after_round, group.set_id, take_id, group.role)
        if found is None:
            raise CrossoverV2Refused({"set_id": group.set_id, "take_id": take_id, "role": group.role},
                                     code=REFUSE_COMPARE_NO_COMPARAND)
        source, before = found.source, side(found.round_dir, found.set_id, found.take_id, found.source)
    else:
        before = side(args.before, args.before_set, args.before_take)
        after = side(after_round, args.after_set, args.after_take)
    return ({**compare_bass_takes(before[0], after[0], change=args.change), "comparand": source,
             "source_views": [before[1], after[1]]},
            resolved_out(after_round, ARTIFACT_BY_VIEW[args.command].artifact, args.after_set), [before[2], after[2]])


def _cmd(args: argparse.Namespace) -> int:
    read: dict[str, Any] | list[dict[str, Any]]
    try:
        if args.command == "bass-compare":
            payload, destination, read = _compare(args)
            summary: dict[str, Any] = {key: payload[key] for key in ("comparison", "comparand", "context", "ladder", "bands")}
            parameters: dict[str, Any] = {"change": args.change}
        else:
            root = args.round_dir if args.command == "bass" else args.round_dir[-1]
            inputs = round_inputs(root)
            destination = default_out(inputs, root, ARTIFACT_BY_VIEW[args.command].artifact,
                                      args.set if args.command == "bass" else None)
            if args.command == "bass":
                payload = bass_payload(inputs, args.set)
                summary = {"takes": len(payload["takes"])}
                read = subject(inputs, set_id=payload["set_id"], candidate_id=payload["candidate_id"])
                parameters = {"calibration_id": calibration_id(payload["takes"][0]["calibration"])}
            else:
                from ._bass_inputs import fit_run  # lazy: laptop array analysis
                payload = fit_run(args)
                levels = [row for table in payload["tables"] for row in table["levels"]]
                summary = {"run_ids": payload["run_ids"], "level_count": len(levels),
                           "levels": bass_table_rows(payload)}
                read = [subject(round_inputs(path)) for path in args.round_dir]
                parameters = {"reference_band_hz": list(args.reference_band_hz)}
    except CrossoverV2Refused as refusal:
        message, action = refusal_copy_for(refusal.code)
        return failed(EXIT_REFUSED, refusal.code, refusal.args[0] if refusal.args else message,
                      code=refusal.code, next_action=action)
    except RoundSetRefused:
        raise
    except OSError as exc:
        return failed(EXIT_UNREADABLE, REASON_UNREADABLE, {"path": exc.filename, "errno": exc.errno})
    except _ROUND_TOOL_ERRORS as exc:
        return failed(EXIT_UNREADABLE, REASON_UNREADABLE, str(exc))
    schema = ARTIFACT_BY_VIEW[args.command].schema
    written = _write(payload, args.out, destination, schema=schema)
    table = bass_table_markdown(summary["levels"]) if args.command == "bass-fit-table" else ""
    return answer(args.command, schema=schema, subject=read, parameters=parameters, out=written, **summary,
                  line=f"{args.command} -> {written}\n{table}".rstrip())
