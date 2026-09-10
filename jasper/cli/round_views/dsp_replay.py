# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The existing file renderer, with explicit Main and bass-reference values."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import wave

from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import ARTIFACT_BY_VIEW, _ROUND_TOOL_ERRORS, _write, answer


def _cmd_replay(args: argparse.Namespace) -> int:
    from jasper.active_speaker.bench.replay import replay_graph  # lazy: isolated native DSP work
    from jasper.active_speaker.bench.render import RenderError  # lazy: native renderer dependencies

    payload = stage(EXIT_UNREADABLE, (*_ROUND_TOOL_ERRORS, RenderError, wave.Error), replay_graph,
                    args.graph, args.stimulus, args.out, main_db=args.main_db, bass_reference_db=args.bass_reference_db)
    written = _write(payload, str(args.out / ARTIFACT_BY_VIEW[args.command].artifact), args.out / "dsp_replay.json")
    return answer(args.command, out=written, output=payload["output"], line=f"dsp-replay -> {written}")


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("dsp-replay", help="render a graph and PCM16 WAV through the installed native DSP; no audio devices")
    parser.add_argument("graph", type=Path)
    parser.add_argument("stimulus", type=Path)
    parser.add_argument("--main-db", type=float, required=True)
    parser.add_argument("--bass-reference-db", type=float, required=True)
    parser.add_argument("--out", type=Path, required=True, help="render directory; use pi-run-diagnostic.sh on the Pi")
    parser.set_defaults(func=_cmd_replay)
    levels = sub.add_parser("dsp-levels", help="read digital bass-band levels from an exact native render (laptop)")
    levels.add_argument("manifest", type=Path)
    levels.add_argument("--raw", type=Path, required=True, help="copied output.f64le")
    levels.add_argument("--window-s", type=float, nargs=2, required=True, metavar=("START", "STOP"))
    levels.add_argument("--out")
    levels.set_defaults(func=_cmd_levels)


def _cmd_levels(args: argparse.Namespace) -> int:
    from jasper.active_speaker.bench.replay import replay_levels  # lazy: laptop FFT analysis

    def levels():
        return replay_levels(json.loads(args.manifest.read_text()), args.raw, tuple(args.window_s))
    payload = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, levels)
    written = _write(payload, args.out, args.manifest.parent / ARTIFACT_BY_VIEW[args.command].artifact)
    return answer(args.command, out=written, channels=payload["channels"], line=f"dsp-levels -> {written}")
