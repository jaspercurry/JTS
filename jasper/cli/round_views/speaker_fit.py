# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Speaker fit proposals and the banked alignment and trim evidence."""

import argparse
from pathlib import Path

from jasper.active_speaker.linearization_budget import DEFAULT_FIT_BUDGET
from jasper.active_speaker.speaker_fit import SpeakerFitUnreadable, speaker_fit
from jasper.cli._refusal import EXIT_UNREADABLE, stage
from ._common import _ROUND_DIR_HELP, _ROUND_DIR_METAVAR, answer, read_run_manifest, round_inputs


def _cmd_speaker_fit(args: argparse.Namespace) -> int:
    inputs = round_inputs(Path(args.round_dir))
    result = stage(EXIT_UNREADABLE, (SpeakerFitUnreadable,), speaker_fit, inputs, read_run_manifest(inputs), args.set, args.take, vocabulary=args.vocabulary,
                         budget={key: getattr(args, key) for key in DEFAULT_FIT_BUDGET if getattr(args, key) is not None})
    return answer(args.command, line=f"speaker-fit: {len(result['linearization'])} driver proposals", **result)


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("speaker-fit", help="propose driver filters and read banked alignment and trims")
    parser.add_argument("round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
    parser.add_argument("--set", required=True, help="manifest set containing the Speaker take")
    parser.add_argument("--take", help="selected take ID when the set holds several takes")
    parser.add_argument("--vocabulary", choices=("cut_only", "bounded_boost"), help="override this round's production vocabulary")
    for key in DEFAULT_FIT_BUDGET:
        parser.add_argument("--" + key.replace("_", "-"), type=int if key == "max_filters" else float,
                            help=f"inspection-only {key} override; leaves banked declarations unchanged")
    parser.set_defaults(func=_cmd_speaker_fit)
