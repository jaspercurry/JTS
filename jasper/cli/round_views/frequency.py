# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The renderer-neutral frequency view, shared with the JTS web page.

* ``frequency <source-a> [<source-b>]`` — the renderer-neutral frequency view
  shared with the JTS web page. A source may be a banked round, a session
  bundle, or a JSON measurement/analysis document. A round's or a bundle's
  curves carry the set and selection its run manifest gives them. A banked
  round reads the view its bank filed, and the view never writes over a file
  it read.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from jasper.active_speaker.frequency_view import frequency_run_from_view
from jasper.active_speaker.frequency_plot import DEFAULT_REF_BAND_HZ
from jasper.active_speaker.measurement_document import frequency_run_from_documents
from jasper.active_speaker.round_view_builders import analyzed_frequency_run, frequency_payload, frequency_image
from jasper.active_speaker.crossover_v2.round_inputs import banked_round_of, round_inputs
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    ARTIFACT_BY_VIEW,
    _ROUND_TOOL_ERRORS,
    _write,
    answer,
    subject,
)

def _frequency_default_out(source: Path) -> Path:
    """:func:`_cmd_frequency`'s own default: it takes sources the round
    resolver does not (a JSON document, a bundle that banked no round), so it
    reads the live shape directly — under the same rule as
    :func:`default_out`: beside the round a bundle was banked into, and never
    inside a daemon-owned session bundle.
    """
    name = ARTIFACT_BY_VIEW["frequency"].artifact
    if not source.is_dir():
        return source.parent / name
    if (source / "info.json").is_file():
        banked_round = banked_round_of(source)
        if banked_round is not None:
            return banked_round / name
        return Path.cwd() / f"{source.name}-{name}"
    return source / name


def _banked_view(source: Path) -> Path | None:
    """The view a bank filed for this banked round, or for the bundle it banked."""
    round_dir = source if (source / "bundle").is_dir() else banked_round_of(source)
    view = round_dir / ARTIFACT_BY_VIEW["frequency"].artifact if round_dir is not None else None
    return view if view is not None and view.is_file() else None


def _frequency_source(path: Path):
    """One round, bundle, or JSON document as a neutral frequency run."""

    if not path.is_file():
        return analyzed_frequency_run(path)
    document = json.loads(path.read_text())
    if not isinstance(document, dict):
        raise ValueError(f"{path}: expected one JSON object")
    view = frequency_run_from_view(document)
    if view is not None:
        return view
    run = frequency_run_from_documents(run_id=path.stem, documents=(document,))
    if not run.series:
        raise ValueError(f"{path}: no usable frequency-response curves")
    return run


def _cmd_frequency(args: argparse.Namespace) -> int:
    sources = [Path(source) for source in (args.source_a, args.source_b) if source]
    # A banked round reads the view its bank filed (#5928 TB5).
    read = [(_banked_view(source) if source.is_dir() else None) or source for source in sources]
    # Resolving a source IS this verb's load stage, "that document holds no
    # curves" included: the fix is to name a different source.
    runs = [stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, _frequency_source, path) for path in read]
    payload, series = frequency_payload(*runs, ref_band_hz=args.ref_band_hz, normalize=args.normalize)
    schema = ARTIFACT_BY_VIEW[args.command].schema
    default = _frequency_default_out(sources[0])
    # Never over a file it read, the bank's own view included.
    written = None if args.out is None and default in read else _write(payload, args.out, default, schema=schema)
    return answer(
        args.command, schema=schema,
        subject=[subject(round_inputs(Path(source))) if Path(source).is_dir() else {}
                 for source in (args.source_a, args.source_b) if source],
        parameters={"ref_band_hz": list(args.ref_band_hz), "normalize": args.normalize},
        out=written, **render_image(args, payload),
        runs=[run["id"] for run in payload["runs"]], series=series,
        line=(
            f"frequency: {len(payload['runs'])} run(s)"
            f"{f' -> {written}' if written else ''}"
        ),
    )


def add_image_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--image", type=Path, help="render PNG/SVG/PDF from this view (requires matplotlib)")
    parser.add_argument("--series", nargs="+", default=(), metavar="SLOT:ID",
                        help="image curves by exact slot:id; omit to show all")
    parser.add_argument("--plot-band-hz", type=float, nargs=2, metavar=("LOW", "HIGH"), help="image frequency range; saved numerical data stays complete")
    parser.add_argument("--ref-band-hz", type=float, nargs=2, default=DEFAULT_REF_BAND_HZ, metavar=("LOW", "HIGH"),
                        help="per-curve power-mean reference band (default: 200–5000 Hz)")
    parser.add_argument("--low-end", action="store_true", help="add a 20–300 Hz panel per pose")
    parser.add_argument("--normalize", action="store_true", help="use each curve's reference-band power mean for shape comparison")


def render_image(args: argparse.Namespace, payload: dict) -> dict:
    return frequency_image(payload, args.image, series=args.series, plot_band_hz=args.plot_band_hz,
                           ref_band_hz=args.ref_band_hz, low_end=args.low_end, normalize=args.normalize)


def add_parser(sub: argparse._SubParsersAction) -> None:
    frequency = sub.add_parser("frequency", help="build the shared frequency-response view")
    frequency.add_argument(
        "source_a", metavar="<source-a>",
        help="banked round, session bundle, or JSON document for A",
    )
    frequency.add_argument(
        "source_b", nargs="?", metavar="<source-b>",
        help="optional banked round, session bundle, or JSON document for B",
    )
    frequency.add_argument("--out", default=None, help="write the result here")
    add_image_args(frequency)
    frequency.set_defaults(func=_cmd_frequency)
