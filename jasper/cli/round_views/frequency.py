# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The renderer-neutral frequency view, shared with the JTS web page.

* ``frequency <source-a> [<source-b>]`` — the renderer-neutral frequency view
  shared with the JTS web page. A source may be a banked round, a session
  bundle, or a JSON measurement/analysis document.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from jasper.active_speaker.crossover_v2.frequency_view import frequency_run
from jasper.active_speaker.frequency_view import SCHEMA, frequency_run_from_view
from jasper.active_speaker.frequency_plot import DEFAULT_REF_BAND_HZ
from jasper.active_speaker.measurement_archive import (
    ArchivedMeasurement,
    load_measurement,
)
from jasper.active_speaker.measurement_document import frequency_run_from_documents
from jasper.active_speaker.round_view_builders import analyzed_frequency_run, frequency_payload, frequency_image
from jasper.active_speaker.crossover_v2.round_inputs import banked_round_of, round_inputs
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    ARTIFACT_BY_VIEW,
    _ROUND_TOOL_ERRORS,
    _load_round,
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


def _frequency_source(
    path: Path, *, analyze_wavs: bool = False, calibration_root: Path | None = None,
    run_reference_db: float | None = None,
):
    """One round, bundle, or JSON document as a neutral frequency run."""

    if analyze_wavs:
        return analyzed_frequency_run(path, calibration_root=calibration_root, run_reference_db=run_reference_db)
    if run_reference_db is not None:
        raise ValueError("--reference-db requires --analyze-wavs")
    if path.is_file():
        document = json.loads(path.read_text())
        if not isinstance(document, dict):
            raise ValueError(f"{path}: expected one JSON object")
        if document.get("schema") == SCHEMA:
            return frequency_run_from_view(document)
        run = frequency_run_from_documents(
            run_id=path.stem, documents=(document,),
        )
    elif (path / "info.json").is_file():
        info = json.loads((path / "info.json").read_text())
        if not isinstance(info, dict):
            raise ValueError(f"{path / 'info.json'}: expected one JSON object")
        run = load_measurement(ArchivedMeasurement(
            id=str(info.get("session_id") or path.name),
            bundle_dir=path,
            started_at=info.get("started_at"),
            state=str(info.get("state") or "") or None,
        ))
    else:
        banked = _load_round(path)
        direct = load_measurement(ArchivedMeasurement(
            id=path.name, bundle_dir=banked.session_dir, started_at=None, state=None,
        ))
        run = direct if any(curve.details.get("phase") == "lateral" and curve.details.get("candidate_id")
                            for curve in direct.series) else frequency_run(banked.packet)
    if not run.series:
        raise ValueError(f"{path}: no usable frequency-response curves")
    return run


def _cmd_frequency(args: argparse.Namespace) -> int:
    source_a = Path(args.source_a)
    # Resolving a source IS this verb's load stage, "that document holds no
    # curves" included: the fix is to name a different source.
    run_a = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, _frequency_source, source_a,
                  analyze_wavs=args.analyze_wavs, calibration_root=args.calibration_root,
                  run_reference_db=args.reference_db)
    run_b = (
        stage(
            EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, _frequency_source, Path(args.source_b),
            analyze_wavs=args.analyze_wavs, calibration_root=args.calibration_root,
            run_reference_db=args.reference_db,
        )
        if args.source_b
        else None
    )
    payload, series = frequency_payload(run_a, run_b, ref_band_hz=args.ref_band_hz, normalize=args.normalize)
    schema = ARTIFACT_BY_VIEW[args.command].schema
    written = _write(payload, args.out, _frequency_default_out(source_a), schema=schema)
    return answer(
        args.command, schema=schema,
        subject=[subject(round_inputs(Path(source))) if Path(source).is_dir() else {}
                 for source in (args.source_a, args.source_b) if source],
        parameters={"ref_band_hz": list(args.ref_band_hz), "normalize": args.normalize,
                    "analyze_wavs": args.analyze_wavs, "reference_db": args.reference_db},
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
    frequency.add_argument("--analyze-wavs", action="store_true", help="analyze captured Room/bass WAVs on this computer (laptop recommended)")
    frequency.add_argument("--calibration-root", type=Path, help="copied microphone calibration registry for the captures’ recorded calibration IDs")
    frequency.add_argument("--reference-db", type=float, help="display reference from a same-level full-band baseline; requires --analyze-wavs")
    frequency.add_argument("--out", default=None, help="write the result here")
    add_image_args(frequency)
    frequency.set_defaults(func=_cmd_frequency)
