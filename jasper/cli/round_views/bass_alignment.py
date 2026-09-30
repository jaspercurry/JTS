# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The sealed-box alignment a curve fits: the corner and Q a Linkwitz transform starts from (#5928 TB9)."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from jasper.active_speaker.bass_fit import bass_alignment
from jasper.active_speaker.crossover_v2.nearfield_view import nearest_raw
from jasper.active_speaker.crossover_v2.position_cycle import measured_curve_band, take_curve
from jasper.active_speaker.crossover_v2.round_inputs import RoundInputs, take_artifact_name
from jasper.audio_measurement.band_ladders import BASS_ALIGNMENT_BAND_HZ
from jasper.audio_measurement.evidence_reasons import TAKE_CURVES_NOT_BANKED, EvidenceUnavailable, unavailable
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    ARTIFACT_BY_VIEW, _ROUND_DIR_HELP, _ROUND_DIR_METAVAR, _ROUND_TOOL_ERRORS, _write, answer, default_out,
    resolve_set, round_inputs, subject,
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
                     "take_ids": raw["take_ids"],
                     **bass_alignment(raw["freqs_hz"], raw["level_db"], args.band_hz, placement["trusted_band"])})
    return subject(inputs, take_ids=[take["take_id"] for take in document["takes"]]), fits


def _take_fits(inputs: RoundInputs, args: argparse.Namespace) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One take's banked curve for its set's role, over the band it can speak for, fitted."""
    selected = resolve_set(inputs, args.set).with_records(inputs.session_dir, every_take=args.take is not None)
    take_id = selected.take_id(args.take)
    take = next(one for one in selected.takes if one["take_id"] == take_id)
    parsed = measured_curve_band(take_curve(take, selected.role, required=True) or {})
    if parsed is None:
        fit = unavailable(TAKE_CURVES_NOT_BANKED, {"field": "curves", "role": selected.role})
    else:
        freqs, level, (low, high) = parsed
        spoken = (freqs >= low) & (freqs <= high)
        fit = bass_alignment(freqs[spoken], level[spoken], args.band_hz, take.get("trusted_band"))
    return subject(inputs, selected, take_ids=[take_id]), [{"role": selected.role, "take_ids": [take_id], **fit}]


def _cmd(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir)
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, round_dir)
    spec = ARTIFACT_BY_VIEW[args.command]
    one_take = args.set is not None or args.take is not None
    read, fits = (_take_fits if one_take else _nearfield_fits)(inputs, args)
    artifact = take_artifact_name(spec.artifact, fits[0]["take_ids"][0], fits[0]["role"]) if one_take else spec.artifact
    if not any(fit["status"] == "available" for fit in fits):
        raise EvidenceUnavailable(fits[0]["reason"], {"fits": fits})
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
    parser.add_argument("--set", help="fit one take of this set as played, not the near-field curves; "
                                      "optional for a one-set round")
    parser.add_argument("--take", help="fit this take of --set as played; defaults to the set's unique on-axis take")
    parser.add_argument("--band-hz", type=float, nargs=2, metavar=("LOW", "HIGH"), default=list(BASS_ALIGNMENT_BAND_HZ),
                        help="band the fit reads, in Hz, clipped to each curve's trusted band "
                             f"(default: {' '.join(f'{hz:g}' for hz in BASS_ALIGNMENT_BAND_HZ)})")
    parser.add_argument("--out", help="artifact destination")
    parser.set_defaults(func=_cmd)
