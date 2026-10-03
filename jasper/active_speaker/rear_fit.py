# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Fit a cardioid rear stage's two branches to a rear/front target.

The target is an ADR-0318 ``acoustic_targets`` document's rear/front ratio.
The front and rear woofers measured alone at one position turn it into the
ELECTRICAL ratio the filters must realize, ``T * H_front / H_rear``, read from
the measured ratio itself so the delay both woofers share cancels. Every
phase is ``positive_delay_has_negative_phase`` -- a delay of T seconds reads
-360*f*T degrees -- so CAD/BEM data solved as exp(-iwt) must have its angle
negated.
"""

from __future__ import annotations

from typing import Any, NamedTuple

import numpy as np
from scipy.optimize import least_squares

from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.evidence_reasons import (
    REFUSE_PAIR_UNDERSAMPLED, REFUSE_TARGET_BAND_SHORT, EvidenceUnavailable,
)
from jasper.audio_measurement.excess_phase import complex_smooth
from jasper.audio_measurement.rear_evidence import magnitude_db
from jasper.dsp_control.camilla_config_contract import DEFAULT_SAMPLE_RATE

from .branch_chain import camilla_filter_response, rear_stage_chain_response, rear_stage_response
from .rear_calibration import (
    KIND, MAX_ALLPASS_Q, MIN_CHAIN_GAIN_DB, PHASE_CONVENTION, RearCalibrationError, read_rear_calibration,
)

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
# A row this far under its own 1/3-octave level sits at a magnitude null,
# where a well-sampled response's phase still turns fast and the ratio spikes
# (#5404 comment 5746999024, item 6).
NULL_DEPTH_DB = 6.0
# The measured ratio's complex mean spans this much octave around each point
# it is read at: wider than the fit grid's step (about 1/37 octave), so room
# detail finer than the fit can follow does not alias into it.
RATIO_SMOOTHING_OCT = 1.0 / 24.0

#: What every fitted document assumes, stated in it.
ASSUMPTIONS = (
    "The fitted electrical ratio is the target acoustic ratio times measured front over measured rear.",
    "SEED, NOT A TUNE: rear_muted is true and the forward response is not verified.",
)

Table = tuple[np.ndarray, np.ndarray]


def acoustic_target(raw: Any) -> Table:
    """``(frequency_hz, rear/front ratio)`` from an ``acoustic_targets`` document
    (ADR-0318) whose valid band covers :data:`FIT_BAND_HZ`. A ``[0, 0]`` rear
    is silent there and carries no phase."""
    document = read_rear_calibration(raw)
    if document["case"] != "acoustic_targets":
        raise RearCalibrationError("the target must be an acoustic_targets document")
    low, high = document["valid_band_hz"]
    if low > FIT_BAND_HZ[0] or high < FIT_BAND_HZ[1]:
        raise EvidenceUnavailable(REFUSE_TARGET_BAND_SHORT,
                                  {"valid_band_hz": [low, high], "fit_band_hz": [*FIT_BAND_HZ]})
    targets = document["targets"]
    front, rear = (np.array([complex(*pair) for pair in targets[side]]) for side in ("front", "rear"))
    if np.any((front == 0) & (rear != 0)):
        raise RearCalibrationError("a moving rear target needs a moving front target")
    ratio = np.divide(rear, front, out=np.zeros_like(rear), where=front != 0)
    return np.asarray(targets["frequency_hz"], dtype=float), ratio


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


class MeasuredRatio(NamedTuple):
    """One measured pair's front/rear ratio, and the rows where neither
    response sits at a magnitude null."""

    freqs_hz: np.ndarray
    ratio: np.ndarray
    kept: np.ndarray

    def on(self, grid: np.ndarray) -> np.ndarray:
        """The kept rows' complex mean around each grid point; a point with no
        kept row near it holds its neighbours' value."""
        values = complex_smooth(self.freqs_hz, self.ratio, RATIO_SMOOTHING_OCT, at=grid, keep=self.kept)
        held = np.isfinite(values)
        return values if held.all() else interpolate((grid[held], values[held]), grid)


def measured_ratio(front: Table, rear: Table) -> MeasuredRatio:
    """One capture's front and rear on one grid, as the ratio a fit reads, or a
    refusal when the grid is too coarse to carry an honest relative phase.

    Past a quarter turn of front/rear phase per row the rotation between rows
    is ambiguous, and no reading between them can say which way it turned. A
    step touching a row at a magnitude null (:data:`NULL_DEPTH_DB`) of either
    response is not that sign, so it is not read, and the null leaves the
    ratio: at a rear null the ratio spikes by the null's depth.
    """
    freqs = front[0]
    if freqs.shape != rear[0].shape or not np.allclose(freqs, rear[0]):
        raise ValueError("measured front and rear must share one frequency grid")
    null = np.zeros(freqs.shape, dtype=bool)
    for values in (front[1], rear[1]):
        level = magnitude_db(values)
        null |= level < smooth_fractional_octave(freqs, level, fraction=3) - NULL_DEPTH_DB
    ratio = front[1] / rear[1]
    band = (freqs >= FIT_BAND_HZ[0]) & (freqs <= FIT_BAND_HZ[1])
    step = np.abs((np.diff(np.angle(ratio[band])) + np.pi) % (2.0 * np.pi) - np.pi)
    step[null[band][:-1] | null[band][1:]] = 0.0
    if step.size and step.max() > np.pi / 2:
        raise EvidenceUnavailable(REFUSE_PAIR_UNDERSAMPLED, {
            "hz": round(float(freqs[band][1:][step.argmax()]), 1), "step_deg": round(float(np.degrees(step.max())), 1)})
    return MeasuredRatio(freqs, ratio, ~null)


def electrical_target(target: Table, measured: MeasuredRatio, grid: np.ndarray) -> np.ndarray:
    """The rear/front ratio the FILTERS must realize, on ``grid``: the target
    times the measured front over the measured rear."""
    return interpolate(target, grid) * measured.on(grid)


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


def chain(gain_db: float, delay_ms: float, filters: list[dict], inverted: bool = False) -> dict[str, Any]:
    """One chain, in the shape the document and the response evaluator share."""
    return {
        "gain_db": float(gain_db), "inverted": inverted, "delay_ms": float(delay_ms),
        "muted": False, "filters": filters,
    }


def combo(kind: str, freq: float, order: int) -> dict[str, Any]:
    return {"type": "BiquadCombo", "parameters": {"type": kind, "freq": float(freq), "order": order}}


def _branches(params: np.ndarray, allpass: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """The ``(bass, cancellation)`` chains a parameter vector describes."""
    cancellation = [
        combo("ButterworthHighpass", params[2], HIGHPASS_ORDER),
        combo("ButterworthLowpass", params[3], LOWPASS_ORDER),
    ]
    if allpass:
        cancellation.append(
            {"type": "Biquad", "parameters": {"type": "Allpass", "freq": float(params[7]), "q": float(params[8])}}
        )
    return (
        chain(params[5], params[1], [combo("ButterworthLowpass", params[0], BASS_ORDER)]),
        chain(params[6], params[4], cancellation, inverted=True),
    )


def branch_ratio(params: np.ndarray, grid: np.ndarray, allpass: bool) -> np.ndarray:
    """The rear/front ratio a parameter vector realizes; the front is unity."""
    bass, cancellation = (
        rear_stage_chain_response(chain, grid, delay_ms=chain["delay_ms"]) for chain in _branches(params, allpass)
    )
    return bass + cancellation


def _residual(
    params: np.ndarray, grid: np.ndarray, target: np.ndarray,
    weights: tuple[np.ndarray, ...], allpass: bool,
) -> np.ndarray:
    return _rows_for(branch_ratio(params, grid, allpass), target, weights)


def group_delay_ms(response: np.ndarray, grid: np.ndarray, freq_hz: float) -> float:
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
            cache[key] = camilla_filter_response([combo(kind, freq, order)], grid)
        return cache[key]

    def turn(delay_ms: float) -> np.ndarray:
        return np.exp(-2j * np.pi * grid * delay_ms / 1e3)

    cardioid_delay = target_delay_ms(target, grid)
    found: list[tuple[float, np.ndarray]] = []
    for bass_corner in np.geomspace(FIT_BAND_HZ[0], 200.0, SEED_POINTS):
        bass_shape = response("ButterworthLowpass", bass_corner, BASS_ORDER)
        bass_delay = -group_delay_ms(bass_shape, grid, BASS_SEED_HZ)
        bass = bass_shape * turn(bass_delay)
        for highpass in np.geomspace(FIT_BAND_HZ[0], 200.0, SEED_POINTS):
            for lowpass in np.geomspace(150.0, FIT_BAND_HZ[1], SEED_POINTS):
                shape = -(
                    response("ButterworthHighpass", highpass, HIGHPASS_ORDER)
                    * response("ButterworthLowpass", lowpass, LOWPASS_ORDER)
                )
                delay = cardioid_delay - group_delay_ms(shape, grid, float(np.mean(DELAY_SLOPE_BAND_HZ)))
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
        "front": emitted(chain(0.0, 0.0, [])),
        "boundary": {"front": [], "rear": []},
        "rear": {"mode": "branches", "bass": emitted(bass), "cancellation": emitted(cancellation)},
    }


def fit_report(document: dict[str, Any], target: Table, measured: MeasuredRatio) -> dict[str, Any]:
    """How near a VALIDATED document comes to the electrical target, and the
    rear's worst level against the front over :data:`SUPPRESSION_SWEEP_HZ`.

    The rows stand at the target's OWN in-band frequencies: interpolating the
    fit grid back onto them would smear a silent row into a tiny non-zero
    target to score against. A silent row has no target level or error.
    """
    freqs = target[0][(target[0] >= FIT_BAND_HZ[0]) & (target[0] <= FIT_BAND_HZ[1])]
    wanted = electrical_target(target, measured, freqs)
    unmuted = {**document, "rear_muted": False}
    summed, front = rear_stage_response(unmuted, freqs)
    achieved = summed / front
    rows = []
    for freq, want, got in zip(freqs, wanted, achieved):
        level, angle = float(magnitude_db(got)), float(np.degrees(np.angle(got)))
        row: dict[str, float | None] = {"hz": float(freq), "achieved_db": level, "achieved_deg": angle,
                                        "target_db": None, "target_deg": None, "error_db": None, "error_deg": None}
        if want != 0:
            want_db, want_deg = float(magnitude_db(want)), float(np.degrees(np.angle(want)))
            row.update(target_db=want_db, target_deg=want_deg, error_db=level - want_db,
                       error_deg=(angle - want_deg + 180.0) % 360.0 - 180.0)
        rows.append({key: None if value is None else round(value, 3) for key, value in row.items()})
    loud, quiet = rear_stage_response(unmuted, SUPPRESSION_SWEEP_HZ)
    worst = float(np.max(magnitude_db(loud / quiet)))
    return {"residuals": rows,
            "suppression": {"max_ratio_db": round(worst, 3), "meets_floor": worst <= -SUPPRESSION_FLOOR_DB}}
