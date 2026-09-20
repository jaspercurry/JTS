#!/usr/bin/env python3
"""Replay a banked PAIR round (regime "branches") through the product's own
branch analysis, per microphone position and per microphone.

Sibling of ``twomic_analyse.py`` (summed trial rounds). Same cut/alignment/
calibration approach for the side mic, same UNGATED window pin
(``MeasurementGeometry(gate_exempt_reason=SEAT_EXEMPT)``). Every number comes
from a product function; this file cuts, loops, tabulates and writes CSV.

Writes, per (mic, pose):
  H_front.csv / H_rear.csv / H_both.csv  -- the three measured segments as
      ``frequency_hz, magnitude_db, phase_deg``, ONE timing reference, on one
      product pair grid.

These are RAW driver transfers, and no chain is divided out of them. A pair
batch plays one candidate the run composed itself: the applied tune with its
rear calibration CLEARED (``rear_views`` module header; this round's
``rear_view.json`` carries ``pair.source.resolution = "cleared"`` and a
``pair.candidate_id`` that is not the applied one). So they are already what
``scripts/fit-rear-branches.py --measured-front/--measured-rear`` and
``rear_preview._position`` both want.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import datetime
from pathlib import Path

import numpy as np

from jasper.active_speaker.crossover_v2.rear_views import PAIR_ROLES, _pair_position
from jasper.active_speaker.crossover_v2.round_captures import doc_pose_key
from jasper.active_speaker.crossover_v2.spatial import analysis_curve_records
from jasper.audio_measurement.analysis import band_levels_from_magnitude, smooth_fractional_octave
from jasper.audio_measurement.branch_program import is_branch_program
from jasper.audio_measurement.calibration import parse_calibration_text
from jasper.audio_measurement.deconv import cap_capture_length
from jasper.audio_measurement.gating import SEAT_EXEMPT
from jasper.audio_measurement.program import ExcitationProgram
from jasper.audio_measurement.program_analysis import MeasurementGeometry, analyze_program_capture
from jasper.audio_measurement.program_analysis.model import CAPTURE_BOUND_MARGIN_S
from jasper.audio_measurement.rear_evidence import FIGURE_FRACTION, IMPULSE_FFT_SIZE, magnitude_db
from jasper.audio_measurement.room_boundary import room_ceiling_hz
from jasper.audio_measurement.wired_capture import decode_wav_to_mono

PRE_ROLL_S = 1.0          # cut starts this far before the journal's action=start
TAIL_S = 3.0              # ... and runs program length + this, before cap_capture_length
START_RE = re.compile(r"^(\S+) .*program_playback.* action=start ")
END_RE = re.compile(r"^(\S+) .*program_playback.* action=end ")

#: CSV band. The grid itself is the product's OWN pair grid -- every
#: ``IMPULSE_FFT_SIZE`` bin inside this band, no subsampling. A log subsample of
#: the 0.0916 Hz analysis grid was tried first and refused by
#: ``fit-rear-branches.check_measured_pair`` (175 degrees of front/rear phase
#: per row): one raw analysis bin per row samples the noise between sweep bins,
#: not the transfer. The pair grid's 105 ms impulse window is what makes the
#: transfer smooth enough to sample at all, and 1.46 Hz bins put a 1 ms arrival
#: gap at 0.5 degrees per row.
CSV_BAND_HZ = (20.0, 1000.0)

#: The band the cardioid null is wanted over.
NULL_BAND_HZ = (100.0, 350.0)

#: How far apart the front/rear phase may step per row before
#: ``fit-rear-branches.check_measured_pair`` refuses the pair. Measured here,
#: reported beside every table, never used to change one: the steps that
#: breach it are at NULLS of the measured ratio, where the phase genuinely
#: flips, so a finer grid or a smoother does not remove them (checked at 1/48
#: to 1/6 octave of delay-compensated complex smoothing: 8 breaches become 2).
#: ``run_fitter.py`` is where that guard is answered.
FIT_PHASE_STEP_CEILING_DEG = 90.0


def fail(message: str) -> None:
    raise SystemExit(f"twomic_pair: {message}")


def bundle_root(round_dir: Path) -> Path:
    """The directory take ``wav_path``s and ``evidence/`` hang off.

    A campaign copy nests them one level down under ``bundle/<round id>/``.
    """
    if (round_dir / "evidence").is_dir():
        return round_dir
    found = sorted(round_dir.glob("bundle/*/evidence"))
    if len(found) != 1:
        fail(f"{round_dir} holds {len(found)} bundles with an evidence tree; expected one")
    return found[0].parent


def take_records(root: Path) -> list[dict]:
    """Every banked take record, in capture order (index, then attempt)."""
    rows = [json.loads(path.read_text()) for path in
            sorted(root.glob("evidence/v1/artifacts/crossover_v2/*/positions/*_take_*.json"))]
    if not rows:
        fail(f"no take records under {root}")
    rows.sort(key=lambda row: (int(row["index"]), int(row["attempt"])))
    return rows


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


def analyse(program: ExcitationProgram, samples: np.ndarray, rate: int, calibration, report):
    """The product's own branch chain, with the summed round's UNGATED pin.

    ``SEAT_EXEMPT`` is what the pair round itself captured under: every banked
    ``branch_diagnostic`` gate reads ``applied: false, exempt_reason: seat``.
    Plain ``MeasurementGeometry()`` would gate and blind the curve below
    ~143 Hz, which is most of the band a cardioid null lives in.
    """
    if not is_branch_program(program):
        fail(f"program {program.program_id[:8]} is not a branch (pair) program")
    return analyze_program_capture(
        program, samples, rate, calibration=calibration,
        geometry=MeasurementGeometry(gate_exempt_reason=SEAT_EXEMPT), capture_report=report)


def take_health(record: dict, analysis, *, mic: str, extra: dict | None = None) -> dict:
    """What says whether THIS capture was located and aligned, per take.

    For the side mic these are the cut's report card: the anchor and locate
    confidences and the residual between where the schedule said each segment
    was and where the correlator found it. A cut landing on the wrong playback,
    or a slipped clock, shows here before it shows anywhere else.
    """
    locates = [loc for loc in analysis.locations if loc.kind != "pilot"]
    sweep = next((loc for loc in analysis.locations if loc.kind == "summed_sweep"), None)
    rate = float(analysis.branch_diagnostic["sample_rate_hz"])
    return {
        "take_id": record["take_id"], "mic": mic, "pose": doc_pose_key(record),
        "attempt": record["attempt"],
        "anchor_confidence": None if analysis.anchor is None else float(analysis.anchor.confidence),
        "min_locate_confidence": min((float(loc.confidence) for loc in locates), default=None),
        "worst_residual_ms": max((abs(loc.residual_samples) for loc in locates), default=0) / rate * 1e3,
        "sweep_located_start": None if sweep is None else int(sweep.located_start),
        "sweep_scheduled_start": None if sweep is None else int(sweep.scheduled_start),
        "epsilon_ppm": float(analysis.branch_diagnostic["clock_epsilon_ppm"]),
        "glitch_detected": bool(analysis.glitch_detected),
        "capture_gaps": (record.get("capture_integrity") or {}).get("capture_gaps"),
        **(extra or {}),
    }


def replayed_record(record: dict, analysis) -> dict:
    """The take record the product's pair reader would have banked from THIS
    analysis: its own curves and its own branch diagnostic, everything else
    (pose, level, calibration reference) carried over from the real take."""
    program = ExcitationProgram.from_dict(record["program"])
    return {**record, "branch_diagnostic": analysis.branch_diagnostic,
            "curves": analysis_curve_records(analysis, program)}


def pair_transfers(diagnostic: dict) -> tuple[np.ndarray, dict]:
    """``(freqs_hz, {role: complex transfer})`` for all THREE pair segments.

    :func:`~jasper.active_speaker.crossover_v2.rear_views.pair_takes`'s own
    recipe, one role wider: the same shared window (the front role's pre-guard
    less 5 ms, to the shortest impulse), the same ``IMPULSE_FFT_SIZE`` transform
    and the same ``exp(+j w shift / rate)`` removal of the schedule's clock
    drift. ``pair_takes`` itself reads only the two branch roles, so the summed
    segment is carried through the identical steps rather than a second recipe.
    """
    rows = {row["role"]: row for row in diagnostic["responses"]}
    missing = [role for role in PAIR_ROLES if role not in rows]
    if missing:
        fail(f"branch diagnostic has no response for {missing}")
    rate = float(diagnostic["sample_rate_hz"])
    start = max(0, int(rows[PAIR_ROLES[0]]["pre_guard_samples"]) - round(0.005 * rate))
    end = min(len(rows[role]["impulse"]) for role in PAIR_ROLES)
    freqs = np.fft.rfftfreq(IMPULSE_FFT_SIZE, 1.0 / rate)
    transfers = {
        role: np.fft.rfft(np.asarray(rows[role]["impulse"], dtype=float)[start:end],
                          n=IMPULSE_FFT_SIZE)
              * np.exp(2j * np.pi * freqs * float(rows[role].get("clock_shift_samples", 0.0)) / rate)
        for role in PAIR_ROLES
    }
    return freqs, transfers


def csv_band(freqs: np.ndarray) -> np.ndarray:
    """The pair grid's bins inside :data:`CSV_BAND_HZ`."""
    return np.flatnonzero((freqs >= CSV_BAND_HZ[0]) & (freqs <= CSV_BAND_HZ[1]))


def phase_step_report(freqs: np.ndarray, front: np.ndarray, rear: np.ndarray) -> dict:
    """``check_measured_pair``'s own measurement over its own band, as a number
    rather than a refusal: worst step, where, and how many rows breach it."""
    band = (freqs >= 40.0) & (freqs <= 800.0)
    relative = np.diff(np.angle(front[band] / rear[band]))
    step = np.degrees(np.abs((relative + np.pi) % (2.0 * np.pi) - np.pi))
    if not step.size:
        return {"worst_deg": None, "worst_hz": None, "breaches": 0, "breach_hz": []}
    breach = freqs[band][1:][step > FIT_PHASE_STEP_CEILING_DEG]
    return {"ceiling_deg": FIT_PHASE_STEP_CEILING_DEG,
            "worst_deg": float(step.max()), "worst_hz": float(freqs[band][1:][step.argmax()]),
            "breaches": int(breach.size), "breach_hz": [round(float(hz), 1) for hz in breach]}


def write_csv(path: Path, freqs: np.ndarray, transfer: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["frequency_hz,magnitude_db,phase_deg"]
    lines += [f"{hz:.6f},{db:.6f},{deg:.6f}" for hz, db, deg in
              zip(freqs, magnitude_db(transfer), np.degrees(np.angle(transfer)))]
    path.write_text("\n".join(lines) + "\n")


def band_level_db(freqs: np.ndarray, transfer: np.ndarray, band_hz) -> float | None:
    """One transfer's level over a half-open band, in the take's own dB unit.

    The product's own reduction --- ``band_levels_from_magnitude`` of the
    1/6-octave level --- so a number here and a number from
    :mod:`jasper.audio_measurement.rear_evidence` or from ``predict_null.py``
    are the same statistic and may be subtracted from each other.
    """
    if np.flatnonzero((freqs >= band_hz[0]) & (freqs < band_hz[1])).size < 3:
        return None
    level = smooth_fractional_octave(freqs, magnitude_db(transfer), fraction=FIGURE_FRACTION)
    return float(band_levels_from_magnitude(freqs, level, ((band_hz[0], band_hz[1]),))[0])


def null_figures(freqs: np.ndarray, transfers: dict) -> dict:
    """``null_potential`` and what the CURRENT tune already achieves here."""
    front, rear, both = (band_level_db(freqs, transfers[role], NULL_BAND_HZ)
                         for role in PAIR_ROLES)
    return {
        "band_hz": list(NULL_BAND_HZ),
        # How much rear level a null NEEDS here: rear drive must reach
        # -H_front/H_rear, so this is the gain the rear branch has to make up.
        "null_potential_db": None if None in (front, rear) else front - rear,
        # What the PAIR TAKE's own sum does: the two RAW woofers in phase, no
        # rear stage anywhere. Not the applied tune, and not a prediction.
        "pair_sum_vs_front_db": None if None in (both, front) else both - front,
        "front_level_db": front, "rear_level_db": rear, "both_level_db": both,
    }



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round-dir", type=Path, required=True)
    parser.add_argument("--main-cal", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--side-wav", type=Path)
    parser.add_argument("--side-start-epoch", type=float,
                        help="unix epoch of the side recording's first sample")
    parser.add_argument("--side-cal", type=Path)
    parser.add_argument("--ceiling-hz", type=float, default=None,
                        help="room ceiling; default: the round's own, else the product fallback")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)

    side_args = (args.journal, args.side_wav, args.side_start_epoch, args.side_cal)
    if any(value is not None for value in side_args) and not all(value is not None for value in side_args):
        parser.error("a side mic needs --journal, --side-wav, --side-start-epoch and --side-cal")

    root = bundle_root(args.round_dir)
    records = take_records(root)
    cals = {"main": parse_calibration_text(args.main_cal.read_text(), sign_convention="response")}
    windows: list[tuple[float, float]] = []
    side = None
    if args.side_wav is not None:
        cals["side"] = parse_calibration_text(args.side_cal.read_text(), sign_convention="response")
        windows = journal_windows(args.journal)
        if len(records) != len(windows):
            fail(f"{len(records)} take records but {len(windows)} journal playbacks; "
                 "a retaken take keeps its own record and its own playback, so these must match")
        side, side_rate = decode_wav_to_mono(args.side_wav.read_bytes())

    ceiling_hz = args.ceiling_hz
    if ceiling_hz is None:
        view = args.round_dir / "rear_view.json"
        banked = (json.loads(view.read_text()).get("comparison", {}).get("ceiling", {}).get("ceiling_hz")
                  if view.is_file() else None)
        ceiling_hz = float(banked) if banked else room_ceiling_hz(None)

    replays: dict[str, list[tuple[dict, object]]] = {}
    health: list[dict] = []
    for index, record in enumerate(records):
        program = ExcitationProgram.from_dict(record["program"])
        wav = (root / record["wav_path"]).read_bytes()
        if len(wav) != record["wav_bytes"]:
            fail(f"{record['take_id']}: main WAV is {len(wav)} bytes, record says {record['wav_bytes']}")
        samples, rate = decode_wav_to_mono(wav)
        analysis = analyse(program, samples, rate, cals["main"], record.get("capture_integrity"))
        replays.setdefault("main", []).append((record, analysis))
        health.append(take_health(record, analysis, mic="main"))
        if side is None:
            continue
        if side_rate != program.sample_rate_hz:
            fail(f"side WAV is {side_rate} Hz, the program is {program.sample_rate_hz} Hz")
        start = windows[index][0]
        first = int(round((start - args.side_start_epoch - PRE_ROLL_S) * side_rate))
        want = program.total_samples + int(round(TAIL_S * side_rate))
        if first < 0 or first + want > side.size:
            fail(f"{record['take_id']}: the cut [{first}, {first + want}) falls outside the "
                 f"{side.size}-sample side recording; check --side-start-epoch")
        cut = cap_capture_length(side[first:first + want], sweep_len=program.total_samples,
                                 sample_rate=side_rate,
                                 max_capture_seconds=program.total_samples / side_rate + CAPTURE_BOUND_MARGIN_S)
        side_analysis = analyse(program, cut, side_rate, cals["side"], None)
        replays.setdefault("side", []).append((record, side_analysis))
        health.append(take_health(record, side_analysis, mic="side", extra={
            "cut_first_sample": first, "journal_start_epoch": start,
            "side_start_epoch": args.side_start_epoch}))

    summary: dict = {
        "round_dir": str(args.round_dir), "bundle_root": str(root), "ceiling_hz": ceiling_hz,
        "csv_grid": {"band_hz": list(CSV_BAND_HZ), "bin_hz": 48000.0 / IMPULSE_FFT_SIZE,
                     "rule": "every rear_evidence.IMPULSE_FFT_SIZE bin in band; transfers built by rear_views.pair_takes recipe"},
        "window": "ungated (MeasurementGeometry(gate_exempt_reason=SEAT_EXEMPT)), as the round captured",
        "figure_source": "crossover_v2.rear_views._pair_position on the REPLAYED curves and "
                         "branch diagnostic; pair_band_levels / superposition_residual_db / "
                         "arrival_gap_ms / rear_polarity come from jasper.audio_measurement.rear_evidence",
        "branches_are_raw": "a pair batch plays the applied tune with its rear calibration "
                            "CLEARED (rear_view.json pair.source.resolution), so no chain is "
                            "divided out of these curves",
        "takes": health,
        "mics": {},
    }
    for mic, rows in replays.items():
        poses: dict[str, list] = {}
        for record, analysis in rows:
            poses.setdefault(doc_pose_key(record), []).append((record, analysis))
        summary["mics"][mic] = {}
        for pose, group in sorted(poses.items()):
            # take_id order, which is what ``rear_views._pair_document`` hands
            # ``_pair_position``: the band figures are the FIRST readable
            # repeat's, so the order decides which repeat they come from.
            group.sort(key=lambda row: str(row[0].get("take_id") or ""))
            figures, _grid = _pair_position(
                [replayed_record(record, analysis) for record, analysis in group], {},
                ceiling_hz=ceiling_hz)
            native, native_transfers = pair_transfers(group[0][1].branch_diagnostic)
            bins = csv_band(native)
            freqs = native[bins]
            transfers = {role: tf[bins] for role, tf in native_transfers.items()}
            out = args.out_dir / mic / pose
            for role, name in zip(PAIR_ROLES, ("H_front", "H_rear", "H_both")):
                write_csv(out / f"{name}.csv", freqs, transfers[role])
            # The CSVs are cut to CSV_BAND_HZ for the fitter; the WHOLE pair
            # grid goes beside them because an impulse-domain figure
            # (rear_evidence.band_limited_impulse) needs every bin up to
            # Nyquist to transform back, and a front-guard band reaches 5 kHz.
            np.savez_compressed(out / "pair.npz", freqs_hz=native,
                                **{name: native_transfers[role] for role, name in
                                   zip(PAIR_ROLES, ("H_front", "H_rear", "H_both"))})
            row = {
                "takes": [record["take_id"] for record, _ in group],
                "pose_kind": group[0][0].get("pose_kind"),
                "mark_distance_m": group[0][0].get("mark_distance_m"),
                "coverage_hz": figures["coverage_hz"], "band_hz": figures["band_hz"],
                "reason": figures["reason"], "bands": figures["bands"],
                "superposition_residual_db": figures["superposition_residual_db"],
                "arrival_gap": figures["arrival_gap"], "rear_polarity": figures["rear_polarity"],
                "null_potential": null_figures(freqs, transfers),
                "csv": {name: str(out / f"{name}.csv") for name in ("H_front", "H_rear", "H_both")},
                "npz": str(out / "pair.npz"),
                "measured_phase_step": phase_step_report(
                    freqs, transfers[PAIR_ROLES[0]], transfers[PAIR_ROLES[1]]),
            }
            summary["mics"][mic][pose] = row
            print(f"{mic:4s} {pose:32s} takes={len(group)} "
                  f"resid={figures['superposition_residual_db']:.4f} dB "
                  f"gap={figures['arrival_gap']['ms']:+.4f} ms "
                  f"pol={figures['rear_polarity']['state']:8s} "
                  f"null_potential={row['null_potential']['null_potential_db']:+.2f} dB "
                  f"pair_sum={row['null_potential']['pair_sum_vs_front_db']:+.2f} dB")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(f"wrote {args.out_dir / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
