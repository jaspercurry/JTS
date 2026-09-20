#!/usr/bin/env python3
"""Replay one banked tuning round through the product analysis chain, twice:
once from each take's own (main-mic) WAV, once from a continuous second-mic
recording cut by the speaker journal. No DSP of its own -- it cuts, loops and
tabulates; every number comes from a product function.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import numpy as np

from jasper.active_speaker.crossover_v2 import capture_dispatch
from jasper.active_speaker.crossover_v2.round_captures import doc_pose_key
from jasper.active_speaker.crossover_v2.spatial import analysis_curve_records
from jasper.audio_measurement.calibration import parse_calibration_text
from jasper.audio_measurement.deconv import cap_capture_length
from jasper.audio_measurement.gating import SEAT_EXEMPT
from jasper.audio_measurement.program import ExcitationProgram
from jasper.audio_measurement.program_analysis import MeasurementGeometry, analyze_program_capture
from jasper.audio_measurement.program_analysis.model import CAPTURE_BOUND_MARGIN_S
from jasper.audio_measurement.rear_evidence import LEVEL_BANDS_HZ, band_level_changes
from jasper.audio_measurement.wired_capture import decode_wav_to_mono

import winscore
from twomic_pair import bundle_root
from winscore import WINDOW_MS, score_db, windowed_energies

PRE_ROLL_S = 1.0          # cut starts this far before the journal's action=start
TAIL_S = 3.0              # ... and runs program length + this, before cap_capture_length
START_RE = re.compile(r"^(\S+) .*program_playback.* action=start ")
END_RE = re.compile(r"^(\S+) .*program_playback.* action=end ")

#: Where a cardioid null is wanted, and how densely ``score_100_350_db`` reads
#: it. A LOG grid, so the score weights octaves rather than bins: the analysis
#: grid is linear, and a plain in-band mean would put two thirds of its weight
#: above 200 Hz.
SCORE_BAND_HZ = (100.0, 350.0)
SCORE_POINTS_PER_OCTAVE = 12
MARKER_RULE = "peak"


def fail(message: str) -> None:
    raise SystemExit(f"twomic_analyse: {message}")


def journal_windows(path: Path) -> list[tuple[float, float]]:
    """The round's ``program_playback`` start/end epochs, in journal order."""
    starts, ends = [], []
    for line in path.read_text().splitlines():
        for regex, sink in ((START_RE, starts), (END_RE, ends)):
            match = regex.match(line)
            if match:
                sink.append(datetime.fromisoformat(match.group(1)).timestamp())
    if len(starts) != len(ends):
        fail(f"journal has {len(starts)} start and {len(ends)} end lines")
    if any(end <= start for start, end in zip(starts, ends)):
        fail("journal start/end pairs are not increasing")
    return list(zip(starts, ends))


def take_records(round_dir: Path) -> list[tuple[Path, dict]]:
    """Every banked take record, in capture order (index, then attempt)."""
    rows = []
    for path in sorted(round_dir.glob("evidence/v1/artifacts/crossover_v2/*/positions/*_take_*.json")):
        record = json.loads(path.read_text())
        rows.append((path, record))
    if not rows:
        fail(f"no take records under {round_dir}")
    rows.sort(key=lambda row: (int(row[1]["index"]), int(row[1]["attempt"])))
    return rows


def analyse(program: ExcitationProgram, samples: np.ndarray, rate: int, calibration, report,
            marker_rule: str = "peak") -> dict:
    """The product's own locate/align/average/deconvolve/calibrate/smooth chain.

    ``SEAT_EXEMPT`` is the geometry
    :func:`~jasper.active_speaker.measurement_analysis.analyzed_measurements`
    uses, so the summed curve is the UNGATED one the round banked into
    ``frequency_view.json``. (Plain ``MeasurementGeometry()`` is the
    capture-time geometry and reproduces ``run_manifest.json``'s gated curve
    instead; that one's validity floor is ~143 Hz, so it cannot see bass.)
    """
    analysis = analyze_program_capture(
        program, samples, rate, calibration=calibration,
        geometry=MeasurementGeometry(gate_exempt_reason=SEAT_EXEMPT), capture_report=report)
    with patch.object(capture_dispatch, "read_output_volume", return_value={}):
        verdict = capture_dispatch.assess(analysis, phase=program.phase, program=program)
    curve = next((row for row in analysis_curve_records(analysis, program) if row["role"] == "summed"), None)
    if curve is None:
        fail("the product banked no summed curve for this capture")
    locates = [loc for loc in analysis.locations if loc.kind != "pilot"]
    sweep = next(loc for loc in analysis.locations if loc.kind == "summed_sweep")
    # The WINDOWED figures ride on the product's own summed response, not on
    # the 121-point banked curve: a window needs every bin to transform back.
    # ``_driver_response`` already referenced this IR to its own direct peak,
    # so the marker below lands near a fixed offset and any wander in it is a
    # fact about the take rather than about the locate anchor.
    summed = analysis.summed_response
    marker, energies = windowed_energies(summed.freqs_hz, summed.complex_tf, rule=marker_rule)
    marker_ok = winscore.marker_usable(marker)
    return {
        "marker_sample": marker, "marker_ms": marker / rate * 1e3,
        "marker_usable": marker_ok, "window_energy": energies,
        "sweep_located_start": int(sweep.located_start), "sweep_scheduled_start": int(sweep.scheduled_start),
        "freqs_hz": list(curve["freqs_hz"]), "magnitude_db": list(curve["magnitude_db"]),
        "band_hz": list(curve["band_hz"]), "validity_floor_hz": curve["validity_floor_hz"],
        "gate_window_ms": curve["gate_window_ms"],
        # ``None`` here is a fact about the program, not a gap: a VERIFY summed
        # program carries ONE ``sweep_verify``, and the product's drift estimator
        # needs two occurrences of a role to measure an in-capture epsilon.
        "epsilon_ppm": None if analysis.drift is None else float(analysis.drift.epsilon_ppm),
        "anchor": None if analysis.anchor is None else {
            "name": analysis.anchor.anchor, "confidence": float(analysis.anchor.confidence)},
        "min_locate_confidence": min((float(loc.confidence) for loc in locates), default=None),
        "worst_residual_ms": max((abs(loc.residual_samples) for loc in locates), default=0) / rate * 1e3,
        "pilot_snr_db": {pilot.role: float(pilot.snr_db) for pilot in analysis.pilots},
        "glitch_detected": bool(analysis.glitch_detected),
        "discontinuity_samples": list(analysis.discontinuity_samples or ()),
        "ok": bool(verdict.ok), "reason": verdict.fault or "", "next": verdict.next,
    }


def mean_curve(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    grid = np.asarray(rows[0]["freqs_hz"], dtype=float)
    for row in rows[1:]:
        if not np.allclose(grid, np.asarray(row["freqs_hz"], dtype=float)):
            fail("takes landed on different analysis grids; refusing to average")
    return grid, np.mean([np.asarray(row["magnitude_db"], dtype=float) for row in rows], axis=0)


def score_100_350_db(grid: np.ndarray, curve: np.ndarray, reference: np.ndarray) -> float:
    """This candidate's mean UNGATED level change against the rear-muted one
    over :data:`SCORE_BAND_HZ`. Negative is a deeper null.

    One number per (mic, pose, candidate): what the cardioid null is FOR. Read
    on the same ungated curves every other figure here is read on, so it says
    nothing a gated view could contradict below its own validity floor.
    """
    lo, hi = SCORE_BAND_HZ
    points = np.geomspace(lo, hi, int(round(np.log2(hi / lo) * SCORE_POINTS_PER_OCTAVE)) + 1)
    return float(np.mean(np.interp(np.log(points), np.log(grid), curve - reference)))


def window_scores(rows: list[dict], reference: list[dict]) -> dict[str, float]:
    """This candidate's windowed level against the rear-muted one, per span.

    Energies are averaged over takes BEFORE the ratio, so a candidate measured
    three times and one measured twice are compared as levels rather than as
    an average of logs.
    """
    out: dict[str, float | None] = {}
    for span in (f"{value:g}" for value in WINDOW_MS):
        held = [row["window_energy"][span] for row in rows
                if row["window_energy"][span] is not None]
        zero = [row["window_energy"][span] for row in reference
                if row["window_energy"][span] is not None]
        out[span] = (score_db(float(np.mean(held)), float(np.mean(zero)))
                     if held and zero else None)
    return out


def candidate_table(per_take: list[dict], mic: str, muted_prefix: str,
                    pose: str | None = None) -> dict:
    """Per candidate: the mean of its kept takes, then the product's band
    levels and ``change_db`` against the rear-muted candidate at this mic.

    ``pose`` restricts to ONE microphone position. A score is per (mic, pose,
    candidate); pooling two positions would average two different sound fields
    into one number. ``None`` pools, which is the same thing on a round that
    holds a single pose.

    A take is kept when the PRODUCT accepted it (``ok``), which is what decides
    between a capture and its retake after a ``capture_overrun``.
    """
    kept: dict[str, list[dict]] = {}
    for row in per_take:
        if row["mic"] == mic and row["ok"] and (pose is None or row["pose"] == pose):
            kept.setdefault(row["candidate_id"], []).append(row)
    if not kept:
        fail(f"{mic}: no take was accepted, nothing to average")
    muted = [cid for cid in kept if cid.startswith(muted_prefix)]
    if len(muted) != 1:
        fail(f"{mic}: --muted {muted_prefix!r} matched {len(muted)} candidates")
    grid, reference = mean_curve(kept[muted[0]])
    coverage = kept[muted[0]][0]["band_hz"]
    out = {}
    for candidate, rows in sorted(kept.items()):
        own_grid, curve = mean_curve(rows)
        if not np.allclose(own_grid, grid):
            fail(f"{mic}: {candidate[:8]} is on a different grid from the muted reference")
        out[candidate] = {
            "takes": [row["take_id"] for row in rows],
            # A score is per (mic, POSE, candidate): every round replayed here
            # so far holds one pose, and this is what says so out loud rather
            # than averaging two of them into one number in silence.
            "poses": sorted({row["pose"] for row in rows}),
            "freqs_hz": [round(float(f), 4) for f in grid],
            "magnitude_db": [round(float(v), 4) for v in curve],
            "bands": band_level_changes(grid, curve, reference_db=reference,
                                        coverage_hz=coverage, bands_hz=LEVEL_BANDS_HZ),
            "score_100_350_db": score_100_350_db(grid, curve, reference),
            "win_score_db": window_scores(rows, kept[muted[0]]),
            "marker_ms": [round(row["marker_ms"], 3) for row in rows],
        }
    return {"muted": muted[0], "coverage_hz": coverage,
            "window_ms": list(WINDOW_MS), "marker_rule": MARKER_RULE,
            "window_rule": "4 ms before the take's own 1-4 kHz envelope peak to +T ms after; "
                           "2 ms rise, falling half-Hann over the last third (coh_check_windowed.py)",
            "score_band_hz": list(SCORE_BAND_HZ),
            "score_points_per_octave": SCORE_POINTS_PER_OCTAVE, "candidates": out}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round-dir", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--side-wav", type=Path, required=True)
    parser.add_argument("--side-start-epoch", type=float, required=True,
                        help="unix epoch of the side recording's first sample")
    parser.add_argument("--side-cal", type=Path, required=True)
    parser.add_argument("--main-cal", type=Path, required=True)
    parser.add_argument("--muted", required=True, help="candidate id prefix of the rear-muted tune")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--marker", choices=("peak", "first"), default="peak",
                        help="1-4 kHz envelope rule for the window marker (winscore.marker_sample)")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)

    global MARKER_RULE
    MARKER_RULE = args.marker
    # A campaign copy nests the evidence tree and the WAVs one level down.
    root = bundle_root(args.round_dir)
    records = take_records(root)
    windows = journal_windows(args.journal)
    if len(records) != len(windows):
        fail(f"{len(records)} take records but {len(windows)} journal playbacks; "
             "a retaken take keeps its own record and its own playback, so these must match")
    cals = {"main": parse_calibration_text(args.main_cal.read_text(), sign_convention="response"),
            "side": parse_calibration_text(args.side_cal.read_text(), sign_convention="response")}
    side, side_rate = decode_wav_to_mono(args.side_wav.read_bytes())

    per_take = []
    for (path, record), (start, end) in zip(records, windows):
        program = ExcitationProgram.from_dict(record["program"])
        common = {"take_id": record["take_id"], "candidate_id": record["candidate_id"],
                  "index": record["index"], "attempt": record["attempt"],
                  "pose": doc_pose_key(record),
                  "journal_start_epoch": start, "journal_end_epoch": end}
        main_wav = (root / record["wav_path"]).read_bytes()
        if len(main_wav) != record["wav_bytes"]:
            fail(f"{record['take_id']}: main WAV is {len(main_wav)} bytes, record says {record['wav_bytes']}")
        samples, rate = decode_wav_to_mono(main_wav)
        per_take.append({**common, "mic": "main", "samples": int(samples.size),
                         **analyse(program, samples, rate, cals["main"], record.get("capture_integrity"), args.marker)})

        if side_rate != program.sample_rate_hz:
            fail(f"side WAV is {side_rate} Hz, the program is {program.sample_rate_hz} Hz")
        first = int(round((start - args.side_start_epoch - PRE_ROLL_S) * side_rate))
        want = program.total_samples + int(round(TAIL_S * side_rate))
        if first < 0 or first + want > side.size:
            fail(f"{record['take_id']}: the cut [{first}, {first + want}) falls outside the "
                 f"{side.size}-sample side recording; check --side-start-epoch")
        cut = cap_capture_length(side[first:first + want], sweep_len=program.total_samples,
                                 sample_rate=side_rate,
                                 max_capture_seconds=program.total_samples / side_rate + CAPTURE_BOUND_MARGIN_S)
        side_row = {**common, "mic": "side", "samples": int(cut.size), "cut_first_sample": first,
                    **analyse(program, cut, side_rate, cals["side"], None, args.marker)}
        # WHICH CAPTURE IS THE GOOD ONE is the round's decision, made on the
        # main microphone; the side cut is a second listener on the SAME
        # playback and inherits it. Its own verdict is kept beside it and never
        # used to drop a take: ``capture_dispatch.assess`` grades the locate
        # ANCHOR, and behind the cabinet that anchor reads 0.0-0.37 confidence
        # (13 of 15 takes here refused as anchor_too_quiet/locate_failed) while
        # the pilot SNR is a healthy 17-23 dB. The windowed score needs neither
        # the anchor nor the product's absolute timing -- the journal places the
        # cut and the take's own 1-4 kHz marker places the window.
        side_row["own_ok"], side_row["own_reason"] = side_row["ok"], side_row["reason"]
        side_row["ok"] = per_take[-1]["ok"]
        per_take.append(side_row)

    payload = {
        "round_dir": str(args.round_dir), "side_wav": str(args.side_wav),
        "side_start_epoch": args.side_start_epoch, "pre_roll_s": PRE_ROLL_S, "tail_s": TAIL_S,
        "take_to_playback_mapping": "strict order: records sorted by (index, attempt) zipped with "
                                    "the journal's program_playback start/end pairs; counts must be equal",
        "band_source": "jasper.audio_measurement.rear_evidence.band_level_changes over LEVEL_BANDS_HZ",
        "takes": per_take,
        "by_candidate": {mic: candidate_table(per_take, mic, args.muted) for mic in ("main", "side")},
        # The per-(mic, pose, candidate) view. ``by_candidate`` above pools the
        # poses and stays for a single-pose round's readers.
        "by_pose": {mic: {pose: candidate_table(per_take, mic, args.muted, pose)
                          for pose in sorted({row["pose"] for row in per_take})}
                    for mic in ("main", "side")},
    }
    args.out.write_text(json.dumps(payload, indent=1) + "\n")
    for row in per_take:
        print(f"{row['take_id'][-9:]} {row['mic']:4s} {row['candidate_id'][:8]} ok={row['ok']!s:5s} "
              f"reason={row['reason'] or '-':16s} eps_ppm={row['epsilon_ppm']} "
              f"anchor={row['anchor'] and round(row['anchor']['confidence'], 3)} "
              f"minloc={row['min_locate_confidence'] and round(row['min_locate_confidence'], 3)}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
