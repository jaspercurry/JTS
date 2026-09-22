#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Fit a cardioid ``jts_rear_calibration`` document to a rear/front target.

    scripts/fit-rear-branches.py --target cardioid_target_full.csv \\
        --out rear_calibration.json --report fit.md

Target CSV: ``frequency_hz``, ``rear_front_ratio_mag_db``,
``rear_front_ratio_phase_dsp_deg``, ``rear_motion_zero`` (true, or a blank
magnitude, means the rear is silent there). Measured CSVs, one mic position and
one timing reference on one frequency grid: ``frequency_hz``, ``magnitude_db``,
``phase_deg``. Given both, the fitted ELECTRICAL target becomes
``T * H_front / H_rear``; without them the acoustic-to-electrical transfer is
taken as the identity (ADR-0318 calls it unknown). Every phase is
``positive_delay_has_negative_phase`` -- a delay of T seconds reads -360*f*T
degrees -- so CAD/BEM data solved as exp(-iwt) must have its angle negated.
Exits non-zero when the fit misses the suppression floor.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

import numpy as np
from scipy.optimize import least_squares

from jasper.active_speaker.branch_chain import (
    camilla_filter_response, rear_branch_sum_headroom_db, rear_stage_chain_response, rear_stage_response,
)
from jasper.active_speaker.rear_calibration import (
    KIND,
    MAX_ALLPASS_Q,
    MIN_CHAIN_GAIN_DB,
    PHASE_CONVENTION,
    compile_rear_stage,
    read_rear_calibration,
)
from jasper.camilla_config_contract import DEFAULT_SAMPLE_RATE

FIT_BAND_HZ = (40.0, 800.0)
FIT_POINTS = 160
BASS_ORDER, HIGHPASS_ORDER, LOWPASS_ORDER = 3, 4, 8
SUPPRESSION_SWEEP_HZ = np.geomspace(500.0, 800.0, 60)
SUPPRESSION_FLOOR_DB = 25.0
SUPPRESSION_HINGE_WEIGHT = 30.0
# Complex weight per band (upper edge Hz, weight), and the bands given EXTRA
# magnitude-error rows on top. No stable causal filter can follow the modelled
# target's phase across the 63-100 Hz reinforcement-to-cardioid switch, and the
# extra rows are what stop a magnitude hole opening while the solve tries. Both
# tables are the surviving report's.
BANDS = ((70.0, 2.0), (100.0, 0.5), (250.0, 4.0), (315.0, 2.5), (450.0, 0.4), (800.0, 5.0))
EXTRA_MAGNITUDE_BANDS_HZ = ((63.0, 110.0), (315.0, 800.0))
MAG_WEIGHT = 2.5
# Gains are PRE-common-shift: the fit may put a branch above unity, and the
# shift then attenuates all three chains equally, which leaves the ratio
# unchanged. The floor is set so a shifted gain cannot pass the document's own.
MAX_FIT_GAIN_DB = 3.0
MIN_FIT_GAIN_DB = MIN_CHAIN_GAIN_DB + MAX_FIT_GAIN_DB
# Parameter vector: bass corner Hz, bass delay ms, cancellation high-pass Hz,
# cancellation low-pass Hz, cancellation delay ms, bass gain dB, cancellation
# gain dB, all-pass corner Hz, all-pass q.
PARAM_LOWER = (20.0, -20.0, 20.0, 50.0, -20.0, MIN_FIT_GAIN_DB, MIN_FIT_GAIN_DB, 20.0, 0.05)
PARAM_UPPER = (800.0, 20.0, 800.0, 2000.0, 20.0, MAX_FIT_GAIN_DB, MAX_FIT_GAIN_DB, 800.0, MAX_ALLPASS_Q)
# The bass branch's delay is seeded flat; the cancellation branch's from the
# target's phase slope across this band, where a cardioid ratio is a real
# transfer rather than a taper.
BASS_SEED_HZ = 55.0
DELAY_SLOPE_BAND_HZ = (100.0, 400.0)
SEED_POINTS = 6
SEED_STARTS = 3
ALLPASS_STARTS = 5
ALLPASS_SEED_Q = 1.0

Table = tuple[np.ndarray, np.ndarray]


def _complex_from_db_deg(magnitude_db: Any, phase_deg: Any) -> np.ndarray:
    return 10.0 ** (np.asarray(magnitude_db, dtype=float) / 20.0) * np.exp(
        1j * np.radians(np.asarray(phase_deg, dtype=float))
    )


def _rows(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(path.read_text().splitlines()))


def _cell(row: dict[str, str], name: str, path: Path) -> float:
    """One required number; blank refuses, because a missing phase read as 0
    degrees enters the fit at full weight.
    """
    text = (row.get(name) or "").strip()
    if not text:
        raise ValueError(f"{path.name} leaves {name} blank")
    return float(text)


def _frequencies(rows: list[dict[str, str]], path: Path) -> np.ndarray:
    values = np.array([_cell(row, "frequency_hz", path) for row in rows], dtype=float)
    if np.any(np.diff(values) <= 0.0):
        raise ValueError(f"{path.name} frequencies must increase")
    return values


def read_target(path: Path) -> Table:
    """``(frequency_hz, complex rear/front ratio)`` from a cardioid target table."""
    rows = _rows(path)
    # The one legitimate blank: a blank magnitude says what ``rear_motion_zero``
    # says, that the rear is silent there and carries no phase either.
    silent = [
        (row.get("rear_motion_zero") or "").strip().lower() == "true"
        or not (row.get("rear_front_ratio_mag_db") or "").strip()
        for row in rows
    ]
    values = np.array([
        0j if quiet else _complex_from_db_deg(
            _cell(row, "rear_front_ratio_mag_db", path),
            _cell(row, "rear_front_ratio_phase_dsp_deg", path),
        )
        for row, quiet in zip(rows, silent)
    ])
    return _frequencies(rows, path), values


def read_response(path: Path) -> Table:
    """``(frequency_hz, complex response)`` from a measured magnitude/phase table."""
    rows = _rows(path)
    return _frequencies(rows, path), _complex_from_db_deg(
        [_cell(row, "magnitude_db", path) for row in rows],
        [_cell(row, "phase_deg", path) for row in rows],
    )


def interpolate(table: Table, grid: np.ndarray) -> np.ndarray:
    """A table on ``grid``: magnitude and UNWRAPPED phase apart, on log frequency.

    Interpolating the complex value cuts the chord across a rotation: 24 dB of
    error on a rear 3 ms behind the front at 1/3 octave, 2 dB on the modelled
    target's own 80-100 Hz switch. Magnitude stays LINEAR, so a zero row is zero.
    """
    known, values = table
    want, source = np.log(np.asarray(grid, dtype=float)), np.log(known)
    return np.interp(want, source, np.abs(values)) * np.exp(
        1j * np.interp(want, source, np.unwrap(np.angle(values)))
    )


def check_measured_pair(front: Table, rear: Table) -> None:
    """Refuse a measured pair too coarse to carry an honest relative phase.

    One capture, so one frequency grid. Past a quarter turn of front/rear phase
    per row the rotation between rows is ambiguous and no interpolation recovers
    it: a 1/3-octave pair a millisecond apart interpolates to 24 dB of error.
    """
    if front[0].shape != rear[0].shape or not np.allclose(front[0], rear[0]):
        raise ValueError("measured front and rear must share one frequency grid")
    band = (front[0] >= FIT_BAND_HZ[0]) & (front[0] <= FIT_BAND_HZ[1])
    relative = np.diff(np.angle(front[1][band] / rear[1][band]))
    step = np.abs((relative + np.pi) % (2.0 * np.pi) - np.pi)
    if step.size and step.max() > np.pi / 2:
        worst = front[0][band][1:][step.argmax()]
        raise ValueError(
            f"measured front/rear phase steps {np.degrees(step.max()):.0f} degrees per row "
            f"at {worst:g} Hz; 90 is the most this can unwrap"
        )


def electrical_target(
    target: Table, measured: tuple[Table, Table] | None, grid: np.ndarray,
) -> np.ndarray:
    """The rear/front ratio the FILTERS must realize, on ``grid``."""
    values = interpolate(target, grid)
    if measured is None:
        return values
    check_measured_pair(*measured)
    return values * interpolate(measured[0], grid) / interpolate(measured[1], grid)


def _weights(grid: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-point row weights: complex, extra-magnitude, suppression hinge.

    The first two are square roots of the band tables, so squaring a row
    recovers the tabulated weight; the hinge is its own weight, used as written.
    """
    complex_weight = np.full(grid.shape, BANDS[-1][1])
    for edge, weight in reversed(BANDS):
        complex_weight[grid < edge] = weight
    magnitude_weight = np.zeros(grid.shape)
    for low, high in EXTRA_MAGNITUDE_BANDS_HZ:
        magnitude_weight[(grid >= low) & (grid <= high)] = MAG_WEIGHT
    hinge = np.where(grid >= SUPPRESSION_SWEEP_HZ[0], SUPPRESSION_HINGE_WEIGHT, 0.0)
    return np.sqrt(complex_weight), np.sqrt(magnitude_weight), hinge


def _rows_for(model: np.ndarray, target: np.ndarray, weights: tuple[np.ndarray, ...]) -> np.ndarray:
    """Least-squares rows for one modelled ratio.

    The hinge is one-sided, only what the rear leaves ABOVE the suppression
    floor, so it steers the solve there and then costs nothing. Least squares
    alone does not imply the floor: a fit 6% cheaper measured 13 dB worse.
    """
    complex_weight, magnitude_weight, hinge = weights
    error = complex_weight * (model - target)
    level = np.abs(model)
    return np.concatenate([
        error.real,
        error.imag,
        magnitude_weight * (level - np.abs(target)),
        hinge * np.maximum(0.0, level - 10.0 ** (-SUPPRESSION_FLOOR_DB / 20.0)),
    ])


def _chain(gain_db: float, delay_ms: float, filters: list[dict], inverted: bool = False) -> dict[str, Any]:
    """One chain, in the shape the document and the response evaluator share."""
    return {
        "gain_db": float(gain_db), "inverted": inverted, "delay_ms": float(delay_ms),
        "muted": False, "filters": filters,
    }


def _combo(kind: str, freq: float, order: int) -> dict[str, Any]:
    return {"type": "BiquadCombo", "parameters": {"type": kind, "freq": float(freq), "order": order}}


def _branches(params: np.ndarray, allpass: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """The ``(bass, cancellation)`` chains a parameter vector describes."""
    cancellation = [
        _combo("ButterworthHighpass", params[2], HIGHPASS_ORDER),
        _combo("ButterworthLowpass", params[3], LOWPASS_ORDER),
    ]
    if allpass:
        cancellation.append(
            {"type": "Biquad", "parameters": {"type": "Allpass", "freq": float(params[7]), "q": float(params[8])}}
        )
    return (
        _chain(params[5], params[1], [_combo("ButterworthLowpass", params[0], BASS_ORDER)]),
        _chain(params[6], params[4], cancellation, inverted=True),
    )


def _model(params: np.ndarray, grid: np.ndarray, allpass: bool) -> np.ndarray:
    """The rear/front ratio a parameter vector realizes; the front is unity."""
    return sum(
        rear_stage_chain_response(chain, grid, delay_ms=chain["delay_ms"])
        for chain in _branches(params, allpass)
    )


def _residual(
    params: np.ndarray, grid: np.ndarray, target: np.ndarray,
    weights: tuple[np.ndarray, ...], allpass: bool,
) -> np.ndarray:
    return _rows_for(_model(params, grid, allpass), target, weights)


def _group_delay_ms(response: np.ndarray, grid: np.ndarray, freq_hz: float) -> float:
    """Group delay of an already-computed response, at the nearest grid point."""
    index = min(max(int(np.argmin(np.abs(grid - freq_hz))), 1), grid.size - 2)
    phase = np.unwrap(np.angle(response[index - 1:index + 2]))
    return float(-(phase[2] - phase[0]) / (2.0 * np.pi * (grid[index + 1] - grid[index - 1])) * 1e3)


def target_delay_ms(target: np.ndarray, grid: np.ndarray) -> float:
    """The delay an INVERTED branch needs, from the target's own phase slope.

    Magnitude-weighted least squares over the band, not one frequency's phase,
    which is far too noisy a seed once the target is measured.
    """
    band = (grid >= DELAY_SLOPE_BAND_HZ[0]) & (grid <= DELAY_SLOPE_BAND_HZ[1])
    freqs, values = grid[band], target[band]
    weight = np.abs(values)
    design = np.stack([np.ones(freqs.size), -2.0 * np.pi * freqs], axis=1) * weight[:, None]
    solution, *_ = np.linalg.lstsq(design, np.unwrap(np.angle(values)) * weight, rcond=None)
    return float(solution[1] * 1e3)


def _solve_gains(target: np.ndarray, weight: np.ndarray, shapes: tuple[np.ndarray, np.ndarray]) -> np.ndarray:
    """The gain pair, in dB, for two fixed unit-gain branch shapes: both gains
    enter the model linearly, so the best pair is a solve, not a search.
    """
    design = weight[:, None] * np.stack(shapes, axis=1)
    pair, *_ = np.linalg.lstsq(
        np.concatenate([design.real, design.imag]),
        np.concatenate([(weight * target).real, (weight * target).imag]),
        rcond=None,
    )
    return 20.0 * np.log10(
        np.clip(pair, 10.0 ** (MIN_FIT_GAIN_DB / 20.0), 10.0 ** (MAX_FIT_GAIN_DB / 20.0))
    )


def _seed(
    grid: np.ndarray, target: np.ndarray, weights: tuple[np.ndarray, ...],
) -> list[np.ndarray]:
    """The best ``SEED_STARTS`` coarse starts, scored on the cached shapes."""
    cache: dict[tuple, np.ndarray] = {}

    def response(kind: str, freq: float, order: int) -> np.ndarray:
        key = (kind, float(freq), order)
        if key not in cache:
            cache[key] = camilla_filter_response([_combo(kind, freq, order)], grid)
        return cache[key]

    def turn(delay_ms: float) -> np.ndarray:
        return np.exp(-2j * np.pi * grid * delay_ms / 1e3)

    cardioid_delay = target_delay_ms(target, grid)
    found: list[tuple[float, np.ndarray]] = []
    for bass_corner in np.geomspace(FIT_BAND_HZ[0], 200.0, SEED_POINTS):
        bass_shape = response("ButterworthLowpass", bass_corner, BASS_ORDER)
        bass_delay = -_group_delay_ms(bass_shape, grid, BASS_SEED_HZ)
        bass = bass_shape * turn(bass_delay)
        for highpass in np.geomspace(FIT_BAND_HZ[0], 200.0, SEED_POINTS):
            for lowpass in np.geomspace(150.0, FIT_BAND_HZ[1], SEED_POINTS):
                shape = -(
                    response("ButterworthHighpass", highpass, HIGHPASS_ORDER)
                    * response("ButterworthLowpass", lowpass, LOWPASS_ORDER)
                )
                delay = cardioid_delay - _group_delay_ms(shape, grid, np.mean(DELAY_SLOPE_BAND_HZ))
                cancellation = shape * turn(delay)
                gains = _solve_gains(target, weights[0], (bass, cancellation))
                linear = 10.0 ** (gains / 20.0)
                cost = float(np.sum(
                    _rows_for(linear[0] * bass + linear[1] * cancellation, target, weights) ** 2
                ))
                found.append(
                    (cost, np.array([bass_corner, bass_delay, highpass, lowpass, delay, *gains]))
                )
    found.sort(key=lambda entry: entry[0])
    return [params for _, params in found[:SEED_STARTS]]


def fit(grid: np.ndarray, target: np.ndarray, *, allpass: bool = True) -> np.ndarray:
    """The fitted parameter vector: corners, delays, gains and the all-pass."""
    weights = _weights(grid)

    def polish(start: Any, with_allpass: bool) -> tuple[float, np.ndarray]:
        bounds = (PARAM_LOWER[:9 if with_allpass else 7], PARAM_UPPER[:9 if with_allpass else 7])
        result = least_squares(
            _residual, np.clip(np.asarray(start, dtype=float)[:len(bounds[0])], *bounds),
            bounds=bounds, x_scale="jac",
            args=(grid, target, weights, with_allpass),
        )
        return float(np.sum(result.fun**2)), result.x

    polished = sorted(
        (polish(start, False) for start in _seed(grid, target, weights)),
        key=lambda item: item[0],
    )
    if not allpass:
        return polished[0][1]
    # The all-pass is the one parameter a single start reliably kills: it walks
    # to its own bound and takes the fit into a worse basin, so each start gets
    # its own polish.
    return min(
        (
            polish([*polished[0][1], freq, ALLPASS_SEED_Q], True)
            for freq in np.geomspace(*FIT_BAND_HZ, ALLPASS_STARTS)
        ),
        key=lambda item: item[0],
    )[1]


def _rounded(item: dict[str, Any]) -> dict[str, Any]:
    """One filter at the emitter's 4-decimal precision, so the document and the
    compiled YAML describe the same filter."""
    return {
        "type": item["type"],
        "parameters": {
            key: value if key in ("type", "order") else round(float(value), 4)
            for key, value in item["parameters"].items()
        },
    }


def build_document(
    params: np.ndarray, *, allpass: bool = True,
    conditions: dict[str, Any] | None = None, assumptions: list[str] | None = None,
) -> dict[str, Any]:
    """The ``electrical_dsp`` document a parameter vector compiles to."""
    bass, cancellation = _branches(params, allpass)
    shift = max(0.0, params[5], params[6])

    def emitted(chain: dict[str, Any]) -> dict[str, Any]:
        return {
            **chain,
            "gain_db": round(chain["gain_db"] - shift, 4),
            "delay_ms": round(chain["delay_ms"], 4),
            "filters": [_rounded(item) for item in chain["filters"]],
        }

    return {
        "kind": KIND,
        "schema": 1,
        "case": "electrical_dsp",
        "sample_rate_hz": DEFAULT_SAMPLE_RATE,
        "phase_convention": PHASE_CONVENTION,
        "geometry": {"cabinet_back_wall_m": None, "sources": {"front": None, "rear": None}, "details": None},
        "reference": {"quantity": "electrical_filter_transfer", "units": "linear output/input", "level": None},
        "conditions": conditions or {},
        "valid_band_hz": [*FIT_BAND_HZ],
        "assumptions": assumptions or [],
        "included_stages": {"front": [], "rear": []},
        "common_delay_ms": round(max(0.0, -float(params[1]), -float(params[4])), 4),
        "rear_muted": True,
        "front": emitted(_chain(0.0, 0.0, [])),
        "boundary": {"front": [], "rear": []},
        "rear": {"mode": "branches", "bass": emitted(bass), "cancellation": emitted(cancellation)},
    }


def report_lines(
    document: dict[str, Any], target: Table, measured: tuple[Table, Table] | None,
) -> tuple[list[str], bool]:
    """``(report, whether it clears the suppression floor)``, read back from the
    VALIDATED document rather than from the fit vector.
    """
    freqs = target[0][(target[0] >= FIT_BAND_HZ[0]) & (target[0] <= FIT_BAND_HZ[1])]
    # At the table's OWN frequencies: interpolating the fit grid back onto them
    # would smear a silent row into a tiny non-zero target to score against.
    wanted = electrical_target(target, measured, freqs)
    unmuted = {**document, "rear_muted": False}
    summed, front = rear_stage_response(unmuted, freqs)
    achieved = summed / front

    def row(*cells: str) -> str:
        return "| " + " | ".join(cells) + " |"

    lines = [
        "# Rear-branch fit residuals",
        "",
        row("Hz", "target dB", "target deg", "achieved dB", "achieved deg", "mag err dB", "phase err deg"),
        "|---|---|---|---|---|---|---|",
    ]
    for freq, want, got in zip(freqs, wanted, achieved):
        level, angle = 20.0 * np.log10(abs(got)), np.degrees(np.angle(got))
        if abs(want) == 0.0:
            cells = ["zero", "—", f"{level:+.2f}", "—", "—", "—"]
        else:
            want_db, want_deg = 20.0 * np.log10(abs(want)), np.degrees(np.angle(want))
            cells = [
                f"{want_db:+.2f}", f"{want_deg:+.1f}", f"{level:+.2f}", f"{angle:+.1f}",
                f"{level - want_db:+.2f}", f"{(angle - want_deg + 180.0) % 360.0 - 180.0:+.1f}",
            ]
        lines.append(row(f"{freq:g}", *cells))
    loud, quiet = rear_stage_response(unmuted, SUPPRESSION_SWEEP_HZ)
    worst = float(np.max(20.0 * np.log10(np.abs(loud / quiet))))
    met = worst <= -SUPPRESSION_FLOOR_DB
    lines += [
        "",
        f"- suppression {worst:.2f} dB over {SUPPRESSION_SWEEP_HZ[0]:g}-{SUPPRESSION_SWEEP_HZ[-1]:g} Hz:"
        f" {'meets' if met else 'MISSES'} the {SUPPRESSION_FLOOR_DB:g} dB floor",
        f"- common_delay_ms {document['common_delay_ms']:g} ms;"
        f" front chain gain {document['front']['gain_db']:+.2f} dB",
        f"- headroom charge as written / unmuted: {rear_branch_sum_headroom_db(document):.3f}"
        f" / {rear_branch_sum_headroom_db(unmuted):.3f} dB",
    ]
    return lines, met


def _provenance(dataset: str, measured: bool) -> tuple[dict[str, Any], list[str]]:
    """The document's ``(conditions, assumptions)``: what was fitted to what."""
    return (
        {
            "dataset": dataset, "fit_band_hz": [*FIT_BAND_HZ], "fit_tool": Path(__file__).name,
            "measured": measured, "timing_reference": "front output of this stage",
        },
        [
            "The fitted electrical ratio is the target acoustic ratio times measured front "
            "over measured rear."
            if measured
            else "Both woofers are ASSUMED identical on identical amplifier channels, so "
            "equal voltage gives equal motion; ADR-0318 states that transfer is unknown.",
            "SEED, NOT A TUNE: rear_muted is true and the forward response is not verified.",
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--target", type=Path, required=True, help="cardioid target table (CSV)")
    parser.add_argument("--out", type=Path, help="where to write the fitted document (JSON)")
    parser.add_argument("--report", type=Path, help="where to write the residual report")
    parser.add_argument("--measured-front", type=Path, help="measured front-alone response (CSV)")
    parser.add_argument("--measured-rear", type=Path, help="measured rear-alone response (CSV)")
    args = parser.parse_args()
    if bool(args.measured_front) != bool(args.measured_rear):
        parser.error("a measured re-fit needs both --measured-front and --measured-rear")

    target = read_target(args.target)
    measured = args.measured_front and (
        read_response(args.measured_front), read_response(args.measured_rear)
    )
    grid = np.geomspace(*FIT_BAND_HZ, FIT_POINTS)
    params = fit(grid, electrical_target(target, measured, grid))
    conditions, assumptions = _provenance(args.target.name, measured is not None)
    document = read_rear_calibration(
        build_document(params, conditions=conditions, assumptions=assumptions),
        sample_rate=DEFAULT_SAMPLE_RATE,
    )
    # Proof that the document compiles; the report reads the document itself.
    compile_rear_stage(document, front_channel=0, rear_channel=2, channel_count=3, tweeter_channel=1)
    lines, met = report_lines(document, target, measured)
    print("\n".join(lines))
    if args.report:
        args.report.write_text("\n".join(lines) + "\n")
    if args.out:
        args.out.write_text(json.dumps(document, indent=4) + "\n")
    # The document and report are written either way — the owner inspects a
    # miss — but a fit under the floor is not a pass.
    if not met:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
