#!/usr/bin/env python3
"""System identification of the REAR acoustic path from measured summed rounds.

Every take of one round, pose and microphone is a complex transfer ``X_i``. The
rear-muted take is ``X_0``, and a candidate differs from it only by what its own
rear chain sent to the rear woofer::

    X_i = X_0 + R(f) * c_rear_i(f)      =>      R_est_i = (X_i - X_0) / c_rear_i

``c_rear_i`` is the PRODUCT's own evaluation of that document's rear sum
(``branch_chain.rear_stage_response``). The front chain is identical in every
document of this search, so it is already inside ``X_0`` and never appears.

``R`` is therefore the acoustic transfer from the rear chain's input to the
microphone, measured. It replaces the pair-round model, which was wrong behind
the cabinet by 6-10 dB with the wrong sign.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from jasper.active_speaker.branch_chain import rear_stage_response
from jasper.active_speaker.crossover_v2.round_captures import doc_pose_key
from jasper.audio_measurement.calibration import parse_calibration_text
from jasper.audio_measurement.deconv import cap_capture_length
from jasper.audio_measurement.gating import SEAT_EXEMPT
from jasper.audio_measurement.program import ExcitationProgram
from jasper.audio_measurement.program_analysis import MeasurementGeometry, analyze_program_capture
from jasper.audio_measurement.program_analysis.model import CAPTURE_BOUND_MARGIN_S
from jasper.audio_measurement.wired_capture import decode_wav_to_mono

from rearpred import section_of
from twomic_analyse import PRE_ROLL_S, TAIL_S, journal_windows, take_records
from twomic_pair import bundle_root

SAMPLE_RATE_HZ = 48000
#: Every take lands on this grid, so takes of different rounds can be compared.
#: 32768 bins at 1.46 Hz; the analysed arrival window is far shorter than the
#: 0.68 s this represents, so truncating to it loses nothing.
N_FFT = 32768
#: The band the takes are aligned on. Above the rear chain's 300 Hz low-pass by
#: more than a decade, so it is IDENTICAL in every candidate of one pose and can
#: only differ by the capture's own clock and level.
ALIGN_BAND_HZ = (1000.0, 4000.0)
#: The band R is identified and reported over.
IDENT_BAND_HZ = (80.0, 400.0)
#: A bin where the driving chain is this far under its own in-band peak carries
#: no information about R -- the quotient there is noise over noise.
DRIVE_FLOOR_DB = 20.0
#: The side cut is placed by the journal's ``action=start`` epoch, whose latency
#: jitter was measured at tens of milliseconds, so the bulk lag is found by
#: CROSS-CORRELATION over this span first and only then refined. An earlier
#: version searched +-1 ms directly and refused 12 takes as "broken cuts" that
#: were merely shifted further than that -- their 1-4 kHz energy was normal.
ALIGN_COARSE_MS = 60.0
ALIGN_SEARCH_MS = 0.25
ALIGN_STEP_MS = 0.002
#: A take whose 1-4 kHz residual against the muted take is worse than this is
#: NOT the same playback seen twice. Above 1 kHz every candidate of one pose is
#: identical by construction, so a take that cannot be matched there has a
#: broken cut, not a different tune. Measured: the good side takes land at
#: -12..-18 dB and the bad ones at -0.1 dB with a 17 dB trim, so the gate has
#: two clear populations to sit between.
ALIGN_RESIDUAL_MAX_DB = -6.0


def circular_spread_deg(angles_deg) -> float:
    """Peak-to-peak of angles, measured about their own circular mean.

    A plain max-minus-min reads 355 deg for a set straddling zero, which is a
    1 degree spread reported as a catastrophe.
    """
    angles = np.radians(np.asarray(angles_deg, dtype=float))
    angles = angles[np.isfinite(angles)]
    if angles.size < 2:
        return float("nan")
    centre = np.angle(np.mean(np.exp(1j * angles)))
    folded = np.degrees((angles - centre + np.pi) % (2.0 * np.pi) - np.pi)
    return float(np.max(folded) - np.min(folded))


def freqs() -> np.ndarray:
    return np.fft.rfftfreq(N_FFT, 1.0 / SAMPLE_RATE_HZ)


def band_mask(lo: float, hi: float) -> np.ndarray:
    grid = freqs()
    return (grid >= lo) & (grid <= hi)


def take_transfers(round_dir: Path, *, main_cal: Path, side_cal: Path) -> list[dict[str, Any]]:
    """Every take of a round as a complex transfer on :func:`freqs`, both mics.

    The side cut inherits the MAIN capture's accept verdict, for the reason
    ``twomic_analyse`` documents: the product's verdict grades a locate anchor
    that is too quiet behind the cabinet, while the pilot SNR is healthy.
    """
    logging.disable(logging.CRITICAL)
    root = bundle_root(round_dir)
    records = take_records(root)
    windows = journal_windows(round_dir / "side" / "journal-takes.txt")
    if len(records) != len(windows):
        raise SystemExit(f"{round_dir.name}: {len(records)} takes, {len(windows)} playbacks")
    epoch = float((round_dir / "side" / "side-start-epoch.txt").read_text().strip())
    side, side_rate = decode_wav_to_mono(
        next((round_dir / "side").glob("side-*.wav")).read_bytes())
    cals = {mic: parse_calibration_text(path.read_text(), sign_convention="response")
            for mic, path in (("main", main_cal), ("side", side_cal))}

    def analyse(program, samples, rate, calibration):
        analysis = analyze_program_capture(
            program, samples, rate, calibration=calibration,
            geometry=MeasurementGeometry(gate_exempt_reason=SEAT_EXEMPT))
        summed = analysis.summed_response
        impulse = np.fft.irfft(summed.complex_tf,
                               n=2 * (np.asarray(summed.freqs_hz).size - 1))
        kept = float(np.sum(impulse[:N_FFT] ** 2) / max(np.sum(impulse ** 2), 1e-30))
        verdict = _verdict(analysis, program)
        return np.fft.rfft(impulse[:N_FFT], n=N_FFT), verdict, kept

    rows: list[dict[str, Any]] = []
    for (path, record), (start, _end) in zip(records, windows):
        program = ExcitationProgram.from_dict(record["program"])
        common = {"round": round_dir.name, "take_id": record["take_id"],
                  "candidate": record["candidate_id"][:8], "pose": doc_pose_key(record),
                  "attempt": record["attempt"]}
        samples, rate = decode_wav_to_mono((root / record["wav_path"]).read_bytes())
        transfer, ok, kept = analyse(program, samples, rate, cals["main"])
        rows.append({**common, "mic": "main", "transfer": transfer, "ok": ok, "kept": kept})
        first = int(round((start - epoch - PRE_ROLL_S) * side_rate))
        want = program.total_samples + int(round(TAIL_S * side_rate))
        cut = cap_capture_length(side[first:first + want], sweep_len=program.total_samples,
                                 sample_rate=side_rate,
                                 max_capture_seconds=program.total_samples / side_rate
                                 + CAPTURE_BOUND_MARGIN_S)
        transfer, own, kept = analyse(program, cut, side_rate, cals["side"])
        rows.append({**common, "mic": "side", "transfer": transfer, "ok": ok,
                     "own_ok": own, "kept": kept})
    return rows


def _verdict(analysis, program) -> bool:
    from unittest.mock import patch

    from jasper.active_speaker.crossover_v2 import capture_dispatch
    with patch.object(capture_dispatch, "read_output_volume", return_value={}):
        return bool(capture_dispatch.assess(analysis, phase=program.phase, program=program).ok)


def align(candidate: np.ndarray, reference: np.ndarray) -> dict[str, Any]:
    """Put ``candidate`` on ``reference``'s time base using 1-4 kHz only.

    One sub-sample delay and one complex trim, chosen to minimise the relative
    residual in :data:`ALIGN_BAND_HZ`. The residual it returns IS the alignment
    quality: the two takes are the same playback of the same front chain, so
    anything left over is capture noise or a real change above 1 kHz.
    """
    grid = freqs()
    inside = band_mask(*ALIGN_BAND_HZ)
    # Bulk lag first: the band-limited impulses cross-correlated, which finds a
    # shift of any size inside ALIGN_COARSE_MS without a fine sweep over it.
    masked = [np.where(inside, one, 0.0) for one in (candidate, reference)]
    impulses = [np.fft.irfft(one, n=N_FFT) for one in masked]
    correlation = np.fft.irfft(np.fft.rfft(impulses[1]) * np.conj(np.fft.rfft(impulses[0])),
                               n=N_FFT)
    span = int(ALIGN_COARSE_MS * 1e-3 * SAMPLE_RATE_HZ)
    lags = np.concatenate([np.arange(0, span + 1), np.arange(N_FFT - span, N_FFT)])
    coarse = lags[int(np.argmax(np.abs(correlation[lags])))]
    coarse_ms = (coarse if coarse <= span else coarse - N_FFT) / SAMPLE_RATE_HZ * 1e3
    want, have = reference[inside], candidate[inside]
    omega = 2.0 * np.pi * grid[inside]
    best = None
    for delay in np.arange(coarse_ms - ALIGN_SEARCH_MS, coarse_ms + ALIGN_SEARCH_MS + 1e-9,
                           ALIGN_STEP_MS):
        shifted = have * np.exp(-1j * omega * delay * 1e-3)
        trim = np.vdot(shifted, want) / max(float(np.vdot(shifted, shifted).real), 1e-30)
        residual = float(np.sum(np.abs(want - trim * shifted) ** 2)
                         / max(float(np.sum(np.abs(want) ** 2)), 1e-30))
        if best is None or residual < best[0]:
            best = (residual, float(delay), trim)
    residual, delay, trim = best
    return {"delay_ms": delay, "coarse_ms": coarse_ms,
            "trim_db": 20.0 * np.log10(abs(trim)),
            "trim_deg": float(np.degrees(np.angle(trim))),
            "residual_db": 10.0 * np.log10(max(residual, 1e-30)),
            "aligned": candidate * trim * np.exp(-2j * np.pi * grid * delay * 1e-3)}


def rear_chain(document: Mapping[str, Any]) -> np.ndarray:
    """One document's rear sum on :func:`freqs` -- the product's own evaluator."""
    return rear_stage_response(section_of(document), freqs())[0]


def estimate_r(aligned: np.ndarray, muted: np.ndarray, drive: np.ndarray) -> np.ndarray:
    """``(X_i - X_0) / c_rear_i``, with the under-driven bins left as ``nan``."""
    inside = band_mask(*IDENT_BAND_HZ)
    level = np.abs(drive)
    floor = float(np.max(level[inside])) * 10.0 ** (-DRIVE_FLOOR_DB / 20.0)
    out = np.full(aligned.shape, np.nan + 0j)
    usable = inside & (level > floor)
    out[usable] = (aligned[usable] - muted[usable]) / drive[usable]
    return out


def robust_mean(estimates: list[np.ndarray]) -> np.ndarray:
    """Median magnitude with a unit-phasor mean phase, per bin.

    Robust rather than a plain mean because one bad take -- a corrupt reference,
    a take whose rear chain barely drives a band -- would otherwise drag every
    bin it touches.
    """
    stack = np.asarray(estimates)
    with np.errstate(invalid="ignore"):
        magnitude = np.nanmedian(np.abs(stack), axis=0)
        unit = stack / np.where(np.abs(stack) > 0, np.abs(stack), 1.0)
        phase = np.angle(np.nansum(np.where(np.isnan(unit), 0.0, unit), axis=0))
    return magnitude * np.exp(1j * phase)


def third_octaves(lo: float = IDENT_BAND_HZ[0], hi: float = IDENT_BAND_HZ[1]):
    from jasper.audio_measurement.analysis import THIRD_OCTAVE_BASS_BANDS_HZ
    return [band for band in THIRD_OCTAVE_BASS_BANDS_HZ if band[0] >= lo and band[1] <= hi]


def band_stats(values: np.ndarray, bands) -> list[tuple[float, float]]:
    """Per band: mean magnitude in dB and mean phase in degrees of a complex curve."""
    grid = freqs()
    out = []
    for low, high in bands:
        inside = (grid >= low) & (grid < high) & np.isfinite(values)
        if not np.any(inside):
            out.append((float("nan"), float("nan")))
            continue
        out.append((float(20.0 * np.log10(np.mean(np.abs(values[inside])))),
                    float(np.degrees(np.angle(np.mean(
                        values[inside] / np.abs(values[inside])))))))
    return out


def residual_floor(residuals: list[np.ndarray], bands) -> dict[tuple, float]:
    """The INCOHERENT power the coherent model cannot explain, per band.

    A null removes what the rear woofer can cancel. It cannot remove street
    noise, the part of the room that does not repeat between takes, or the
    non-linear part of the dynamic bass block. Those add POWER, not pressure,
    so the honest forward model is ``|X|^2 = |X_0 + R c|^2 + N^2``. Without the
    ``N^2`` term a predictor is free to promise an arbitrarily deep null, which
    is exactly the systematic over-prediction seen on ident-B and ident-C.

    ``residuals`` are ``X_i - X_0 - R c_i`` for every candidate take the model
    was identified from. The MEDIAN over candidates and bins is used, so one
    bad take cannot inflate the floor.
    """
    grid = freqs()
    out = {}
    stack = np.asarray(residuals)
    for low, high in bands:
        inside = (grid >= low) & (grid < high)
        cell = np.abs(stack[:, inside]) ** 2
        cell = cell[np.isfinite(cell)]
        out[(low, high)] = float(np.median(cell)) if cell.size else 0.0
    return out


def floor_curve(floor: dict[tuple, float]) -> np.ndarray:
    """A per-bin power floor from the per-band estimate."""
    grid = freqs()
    out = np.zeros(grid.shape)
    for (low, high), value in floor.items():
        out[(grid >= low) & (grid < high)] = value
    return out


def load_documents(index: Path, root: Path) -> dict[str, dict[str, Any]]:
    """``{fingerprint8: document}`` from the search's own fingerprint index.

    The index's paths are relative to the scratch ROOT, not to itself.
    """
    return {key: json.loads((root / value).read_text())
            for key, value in json.loads(index.read_text()).items()}
