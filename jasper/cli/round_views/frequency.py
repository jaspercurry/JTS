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

from jasper.active_speaker.commissioning_evidence_store import CommissioningEvidenceStoreError
from jasper.active_speaker.crossover_v2.frequency_view import frequency_run
from jasper.active_speaker.frequency_view import FrequencyRun, build_frequency_view, frequency_series
from jasper.active_speaker.frequency_plot import render_frequency_view
from jasper.active_speaker.measurement_archive import (
    ArchivedMeasurement,
    load_measurement,
)
from jasper.active_speaker.measurement_document import frequency_run_from_documents
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    ARTIFACT_BY_VIEW,
    _ROUND_TOOL_ERRORS,
    _load_round,
    _write,
    answer,
    banked_round_of,
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


def _frequency_source(path: Path, *, analyze_wavs: bool = False, calibration_root: Path | None = None):
    """One round, bundle, or JSON document as a neutral frequency run."""

    if analyze_wavs:
        from jasper.active_speaker.measurement_analysis import MeasurementAnalysisRefused, analyze_measurement_bundle  # lazy: laptop FFT analysis
        try:
            return analyze_measurement_bundle(path, calibration_root=calibration_root)
        except CommissioningEvidenceStoreError as exc:
            raise MeasurementAnalysisRefused(exc.code.value) from exc
    if path.is_file():
        document = json.loads(path.read_text())
        if not isinstance(document, dict):
            raise ValueError(f"{path}: expected one JSON object")
        if document.get("schema") == "jts_frequency_view/1":
            if len(document["runs"]) != 1:
                raise ValueError("as an input, a frequency view must contain one run")
            raw = document["runs"][0]
            curves = []
            for item in raw["series"]:
                fields = dict(item)
                fields["series_id"] = fields.pop("id")
                curve = frequency_series(**fields)
                if curve is not None:
                    curves.append(curve)
            return FrequencyRun(
                id=raw["id"], label=raw.get("label", ""),
                measurement_family=raw["measurement_family"], series=tuple(curves),
                metadata=raw.get("metadata", {}),
            )
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
                  analyze_wavs=args.analyze_wavs, calibration_root=args.calibration_root)
    run_b = (
        stage(
            EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, _frequency_source, Path(args.source_b),
            analyze_wavs=args.analyze_wavs, calibration_root=args.calibration_root,
        )
        if args.source_b
        else None
    )
    payload = build_frequency_view(run_a, run_b)
    written = _write(payload, args.out, _frequency_default_out(source_a))
    image = render_image(args, payload)
    return answer(
        args.command, out=written, image=image, runs=[run["id"] for run in payload["runs"]],
        line=(
            f"frequency: {len(payload['runs'])} run(s)"
            f"{f' -> {written}' if written else ''}"
        ),
    )


def add_image_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--image", type=Path, help="render PNG/SVG/PDF from this view (laptop: matplotlib)")
    parser.add_argument("--series", nargs="+", default=(), metavar="SLOT:ID",
                        help="image curves by exact slot:id; omit to show all")
    parser.add_argument("--plot-band-hz", type=float, nargs=2, metavar=("LOW", "HIGH"), help="image frequency range; saved numerical data stays complete")


def render_image(args: argparse.Namespace, payload: dict) -> str | None:
    if args.image is None:
        return None
    render_frequency_view(payload, args.image, selected=args.series, band_hz=args.plot_band_hz)
    return str(args.image)


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
    frequency.add_argument("--out", default=None, help="write the result here (- for stdout)")
    add_image_args(frequency)
    frequency.set_defaults(func=_cmd_frequency)
