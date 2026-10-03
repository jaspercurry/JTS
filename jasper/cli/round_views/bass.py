# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Bass views."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused, refusal_copy_for
from jasper.active_speaker.round_view_builders import bass_payload
from jasper.cli._refusal import EXIT_REFUSED, EXIT_UNREADABLE, failed

from ._common import (
    ARTIFACT_BY_VIEW, REASON_UNREADABLE, RoundSetRefused, _ROUND_DIR_HELP, _ROUND_DIR_METAVAR, _ROUND_TOOL_ERRORS,
    _write, add_set_argument, answer, calibration_id, round_inputs, set_view_out, subject,
)


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("bass", help="bass response, quiet-window SNR and H2/H3")
    parser.add_argument("--out", help="artifact destination")
    parser.set_defaults(func=_cmd)
    parser.add_argument("round_dir", type=Path, metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
    add_set_argument(parser)


def _cmd(args: argparse.Namespace) -> int:
    try:
        inputs = round_inputs(args.round_dir)
        destination = set_view_out(inputs, ARTIFACT_BY_VIEW[args.command].artifact, args.set)
        payload = bass_payload(inputs, args.set)
        read: dict[str, Any] = subject(inputs, set_id=payload["set_id"], candidate_id=payload["candidate_id"])
        parameters = {"calibration_id": calibration_id(payload["takes"][0]["calibration"])}
    except CrossoverV2Refused as refusal:
        return failed(EXIT_REFUSED, refusal.code, refusal.args[0] if refusal.args else refusal_copy_for(refusal.code)[0],
                      code=refusal.code)
    except RoundSetRefused:
        raise
    except OSError as exc:
        return failed(EXIT_UNREADABLE, REASON_UNREADABLE, {"path": exc.filename, "errno": exc.errno})
    except _ROUND_TOOL_ERRORS as exc:
        return failed(EXIT_UNREADABLE, REASON_UNREADABLE, str(exc))
    schema = ARTIFACT_BY_VIEW[args.command].schema
    written = _write(payload, args.out, destination, schema=schema)
    return answer(args.command, schema=schema, subject=read, parameters=parameters, out=written,
                  takes=len(payload["takes"]), line=f"{args.command} -> {written}")
