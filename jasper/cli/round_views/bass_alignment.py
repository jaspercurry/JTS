# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The sealed-box alignment a curve fits: the corner and Q a Linkwitz transform starts from (ADR-0398)."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

from jasper.active_speaker.bass_fit import bass_alignment
from jasper.active_speaker.crossover_v2.contracts import DRIVER_ROLE_WOOFER
from jasper.active_speaker.crossover_v2.nearfield_view import nearest_raw
from jasper.active_speaker.crossover_v2.pose_curve import WINDOW_UNGATED
from jasper.active_speaker.crossover_v2.position_cycle import curve_band, measured_curve_band, take_curve
from jasper.active_speaker.crossover_v2.round_inputs import RoundInputs, take_artifact_name
from jasper.audio_measurement.band_ladders import BASS_ALIGNMENT_BAND_HZ
from jasper.audio_measurement.evidence_reasons import TAKE_CURVES_NOT_BANKED, EvidenceUnavailable, unavailable
from jasper.audio_measurement.trusted_band import banked_band
from jasper.cli._refusal import EXIT_UNREADABLE, stage
from jasper.platform.speaker_layout import measurement_target_parts

from ._common import (
    ARTIFACT_BY_VIEW, _ROUND_DIR_HELP, _ROUND_DIR_METAVAR, _ROUND_TOOL_ERRORS, _write, add_set_argument, answer,
    default_out, resolve_set_take, round_inputs, subject,
)
from .nearfield import round_nearfield

#: What each fit files beside its numbers; the answer leaves them to the artifact.
_CURVES = ("freqs_hz", "measured_db", "model_db")


def _nearfield_fits(inputs: RoundInputs, args: argparse.Namespace) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Each driver's raw near-field curve at its nearest placement, fitted."""
    _, document = round_nearfield(inputs)
    fits = []
    for driver in document["drivers"]:
        placement = nearest_raw(driver)
        if placement is None:
            fits.append({"role": driver["driver"], **unavailable(TAKE_CURVES_NOT_BANKED, {
                "field": "raw", "take_ids": [one for place in driver["placements"] for one in place["take_ids"]]})})
            continue
        raw = placement["raw"]
        fits.append({"role": driver["driver"], "distance_mm": placement["distance_mm"], "kind": placement["kind"],
                     "take_ids": raw["take_ids"], **bass_alignment(raw["freqs_hz"], raw["level_db"], args.band_hz,
                                                                   banked_band(placement["trusted_band"]))})
    return subject(inputs, take_ids=[take["take_id"] for take in document["takes"]]), fits


def _spoken(curve: Mapping[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    """A banked curve over the band its take can speak for; one that cannot be read raises."""
    parsed = measured_curve_band(curve)
    if parsed is None:
        raise ValueError(f"the banked {curve.get('role')} curve is unreadable")
    freqs, level, (low, high) = parsed
    spoken = (freqs >= low) & (freqs <= high)
    return freqs[spoken], level[spoken]


def _take_fits(round_dir: Path, args: argparse.Namespace) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One take's banked ungated curve for its set's role, fitted as played
    through the room, inside the band banked on that curve."""
    read, take_id, role, take = resolve_set_take(round_dir, args.set, args.take, None)
    curve = take_curve(take, role, WINDOW_UNGATED, required=True)
    freqs, level = stage(EXIT_UNREADABLE, (ValueError,), _spoken, curve)
    return read, [{"role": role, "take_ids": [take_id],
                   **bass_alignment(freqs, level, args.band_hz, curve_band(take, curve))}]


def _refusal_reason(fits: list[dict[str, Any]]) -> str:
    """The reason every gap shares, else a woofer's: the woofers are what the fit is for."""
    reasons = {fit["reason"] for fit in fits}
    if len(reasons) == 1:
        return reasons.pop()
    return next((fit["reason"] for fit in fits if measurement_target_parts(fit["role"])[0] == DRIVER_ROLE_WOOFER),
                fits[0]["reason"])


def _cmd(args: argparse.Namespace) -> int:
    if not 0 < args.band_hz[0] < args.band_hz[1]:
        args.parser.error("--band-hz needs 0 < LOW < HIGH")
    round_dir = Path(args.round_dir)
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, round_dir)
    spec = ARTIFACT_BY_VIEW[args.command]
    if args.set is None and args.take is None:
        read, fits = _nearfield_fits(inputs, args)
        artifact = spec.artifact
    else:
        read, fits = _take_fits(round_dir, args)
        artifact = take_artifact_name(spec.artifact, fits[0]["take_ids"][0], fits[0]["role"])
    if not any(fit["status"] == "available" for fit in fits):
        raise EvidenceUnavailable(_refusal_reason(fits), {"fits": fits})
    parameters = {"band_hz": args.band_hz}
    written = _write({"round_dir": str(round_dir), "parameters": parameters, "fits": fits}, args.out,
                     default_out(inputs, round_dir, artifact), schema=spec.schema)
    return answer(
        args.command, schema=spec.schema, subject=read, parameters=parameters, out=written,
        fits=[{key: value for key, value in fit.items() if key not in _CURVES} for fit in fits],
        line="bass-alignment: " + "; ".join(
            f"{fit['role']} {fit['source_hz']:g} Hz, Q {fit['source_q']:g}, rms {fit['residual_db']:g} dB"
            if fit["status"] == "available" else f"{fit['role']} {fit['reason']}" for fit in fits) + f" -> {written}",
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("bass-alignment", help="the sealed-box corner and Q a curve fits: the Linkwitz transform's source")
    parser.add_argument("round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
    add_set_argument(parser, take=True)
    parser.add_argument("--band-hz", type=float, nargs=2, metavar=("LOW", "HIGH"), default=list(BASS_ALIGNMENT_BAND_HZ),
                        help="band the fit reads, in Hz, clipped to each curve's trusted band "
                             f"(default: {' '.join(f'{hz:g}' for hz in BASS_ALIGNMENT_BAND_HZ)})")
    parser.add_argument("--out", help="artifact destination")
    parser.set_defaults(func=_cmd, parser=parser)
