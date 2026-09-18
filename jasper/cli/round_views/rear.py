# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The rear comparison a finished ``rear`` round banked, read back exactly as
``packet.json`` carries it -- no recomputation, and no ``--set``: the batch
spans one manifest set per played candidate, so it reads the whole round."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from jasper.active_speaker.round_packet_report import PACKET_FILENAME
from jasper.active_speaker.round_view_artifacts import ARTIFACT_BY_VIEW
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    _ROUND_DIR_HELP, _ROUND_DIR_METAVAR, _ROUND_TOOL_ERRORS,
    answer, default_out, refused_by_name, round_inputs,
)

#: This family's own "no data" refusal, in the room/delay convention
#: (``room_no_seat_takes``, ``delay_confirm_no_measured_rows``) rather than
#: the generic stage bucket.
REFUSE_NO_REAR = "rear_no_entries"


def _read_packet(path: Path) -> Mapping[str, Any]:
    return json.loads(path.read_text())


def _cmd_rear(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir)
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, round_dir)
    packet_path = default_out(inputs, round_dir, PACKET_FILENAME)
    packet = stage(EXIT_UNREADABLE, (OSError, ValueError), _read_packet, packet_path)
    entries = packet.get("rear") or []
    if not entries:
        return refused_by_name(REFUSE_NO_REAR, {"round_dir": str(round_dir)})
    out = default_out(inputs, round_dir, ARTIFACT_BY_VIEW["rear"].artifact)
    return answer(
        args.command, out=out, entries=entries,
        line=f"rear: {len(entries)} batch(es) -> {out}",
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "rear",
        help="the rear comparison a finished rear round banked, exactly as the packet embeds it",
    )
    parser.add_argument("round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
    parser.set_defaults(func=_cmd_rear)
