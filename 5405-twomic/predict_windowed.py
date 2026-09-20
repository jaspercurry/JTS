#!/usr/bin/env python3
"""Windowed predictions for a SUMMED trial round, from the pair model.

Every candidate of the round is predicted against the one the round measures
against -- the rear-muted candidate -- through :mod:`winscore`'s window, so the
prediction and the measurement (``twomic_analyse.py``'s ``win_score_db``) are
the same number read the same way and may be put side by side.

The window's marker comes from the MUTED prediction at each position, and every
candidate at that position is then read inside that one window. That is the
point of the marker band: the rear chain is low-passed at 300 Hz, so 1-4 kHz
does not move when the rear candidate changes, and a candidate cannot buy a
score by shifting its own window.
"""
from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from jasper.active_speaker.branch_chain import rear_stage_chain_response, rear_stage_response
from jasper.active_speaker.camilla_yaml import rear_branch_sum_headroom_db

from rearpred import load_positions, predicted_pair, score_position, section_of
from winscore import (
    WINDOW_MS, impulse_of, marker_sample, score_db, windowed_energy,
)

LANDSCAPE_SPAN_MS = 12.0


def label(row: Mapping[str, Any]) -> str:
    degrees = float(row["pose"].split("az")[1].split("_")[0])
    return ("rear" if row["mic"] == "side" else "front") + (
        f"{-degrees:+03.0f}" if row["mic"] == "side" else f"{degrees:+03.0f}")


def reference_of(positions: Mapping[str, Any], muted: Mapping[str, Any],
                 rule: str) -> dict[str, Any]:
    """Per position: the muted curve, its marker, and its windowed energies."""
    out = {}
    for key, row in positions.items():
        curve, _ = predicted_pair(muted, row["freqs_hz"], row["H_front"], row["H_rear"])
        impulse = impulse_of(row["freqs_hz"], curve)
        marker = marker_sample(row["freqs_hz"], curve, rule=rule)
        out[key] = {"marker": marker, "marker_ms": marker / 48.0, "muted": curve,
                    "energy": {f"{span:g}": windowed_energy(impulse, marker, span)
                               for span in WINDOW_MS}}
    return out


def windowed_scores(section: Mapping[str, Any], row: Mapping[str, Any],
                    reference: Mapping[str, Any]) -> dict[str, float]:
    _muted, predicted = predicted_pair(section, row["freqs_hz"], row["H_front"], row["H_rear"])
    impulse = impulse_of(row["freqs_hz"], predicted)
    return {span: score_db(windowed_energy(impulse, reference["marker"], float(span)),
                           reference["energy"][span])
            for span in (f"{value:g}" for value in WINDOW_MS)}


def landscape(base: Mapping[str, Any], positions: Mapping[str, Any],
              reference: Mapping[str, Any], rear_keys) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """N1's structure over gain x cancellation delay, scored at T=12 ms."""
    gains = np.arange(0.0, -8.5, -1.0)
    delays = np.arange(-0.3, 0.71, 0.1)
    span = f"{LANDSCAPE_SPAN_MS:g}"
    shared = float(base["common_delay_ms"]) + float(base["front"]["delay_ms"])
    grid = next(iter(positions.values()))["freqs_hz"]
    bass = rear_stage_chain_response(base["rear"]["bass"], grid,
                                     delay_ms=shared + float(base["rear"]["bass"]["delay_ms"]))
    front = rear_stage_chain_response(base["front"], grid, delay_ms=shared,
                                      extra_filters=base["boundary"]["front"])
    with_bass = {key: positions[key]["H_front"] * front + positions[key]["H_rear"] * bass
                 for key in rear_keys}
    out = np.zeros((delays.size, gains.size))
    for row, delay in enumerate(delays):
        chain = {**deepcopy(base["rear"]["cancellation"]), "delay_ms": float(delay)}
        for column, gain in enumerate(gains):
            response = rear_stage_chain_response({**chain, "gain_db": float(gain)}, grid,
                                                 delay_ms=shared + float(delay))
            scores = []
            for key in rear_keys:
                predicted = with_bass[key] + positions[key]["H_rear"] * response
                scores.append(score_db(
                    windowed_energy(impulse_of(grid, predicted), reference[key]["marker"],
                                    LANDSCAPE_SPAN_MS),
                    reference[key]["energy"][span]))
            out[row, column] = float(np.mean(scores))
    return gains, delays, out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True, help="a twomic_pair.py out-dir")
    parser.add_argument("--muted", type=Path, required=True, help="the rear-muted document")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--marker", choices=("peak", "first"), default="peak",
                        help="which marker rule the printed table uses; BOTH are written out")
    parser.add_argument("docs", type=Path, nargs="+")
    args = parser.parse_args()

    positions = load_positions(args.out_dir)
    keys = sorted(positions, key=lambda key: (positions[key]["mic"] != "side", key))
    rear_keys = [key for key in keys if positions[key]["mic"] == "side"]
    muted = section_of(json.loads(args.muted.read_text()))
    rules = ("peak", "first")
    references = {rule: reference_of(positions, muted, rule) for rule in rules}
    reference = references[args.marker]

    print("predicted level against the rear-muted candidate, dB. Negative is quieter.")
    print("  window: 4 ms before the MUTED prediction's 1-4 kHz envelope peak to +T ms after")
    for rule in rules:
        print(f"  marker ({rule}) per position, ms: "
              + "  ".join(f"{label(positions[key])} {references[rule][key]['marker_ms']:5.2f}"
                          for key in keys))
    print("  candidate       T   " + "".join(f"{label(positions[key]):>9s}" for key in keys)
          + "   headroom")
    payload: dict[str, Any] = {
        "out_dir": str(args.out_dir), "muted": str(args.muted),
        "model": "rearpred.predicted_pair (rear_preview._position) on pair round e4cd4da24c24",
        "window_ms": list(WINDOW_MS), "marker_rule_printed": args.marker,
        "marker_ms_by_rule": {rule: {label(positions[key]): references[rule][key]["marker_ms"]
                                     for key in keys} for rule in rules},
        "positions": {key: {"label": label(positions[key]), **{
            name: positions[key][name] for name in ("mic", "pose")},
            "marker_ms": reference[key]["marker_ms"]} for key in keys},
        "candidates": {},
    }
    for path in args.docs:
        section = section_of(json.loads(path.read_text()))
        both = {rule: {key: windowed_scores(section, positions[key], references[rule][key])
                       for key in keys} for rule in rules}
        rows = both[args.marker]
        ungated = {key: score_position(section, positions[key])["null_band_db"] for key in keys}
        charge = rear_branch_sum_headroom_db(section)
        for index, span in enumerate(f"{value:g}" for value in WINDOW_MS):
            print(f"  {path.stem if index == 0 else '':14s} {span:>3s}  "
                  + "".join(f"{rows[key][span]:+9.2f}" for key in keys)
                  + (f"   {charge:.3f} dB" if index == 0 else ""))
        print(f"  {'':14s} ung  " + "".join(f"{ungated[key]:+9.2f}" for key in keys))
        payload["candidates"][path.stem] = {
            "document": str(path), "headroom_charge_db": charge,
            "win_score_db": {label(positions[key]): rows[key] for key in keys},
            "win_score_db_by_marker": {rule: {label(positions[key]): both[rule][key]
                                              for key in keys} for rule in rules},
            "ungated_db": {label(positions[key]): ungated[key] for key in keys},
        }

    base = section_of(json.loads(args.docs[0].read_text()))
    gains, delays, values = landscape(base, positions, reference, rear_keys)
    print(f"\n  model landscape, N1 structure, T={LANDSCAPE_SPAN_MS:g} ms, "
          f"mean of the 3 rear angles, marker rule {args.marker}")
    print("    delay\\gain " + "".join(f"{gain:7.0f}" for gain in gains))
    for row, delay in enumerate(delays):
        print(f"    {delay:+6.2f}    " + "".join(f"{value:7.2f}" for value in values[row]))
    best = np.unravel_index(int(np.argmin(values)), values.shape)
    print(f"    minimum {values[best]:+.2f} dB at gain {gains[best[1]]:+.0f} dB, "
          f"delay {delays[best[0]]:+.2f} ms")
    payload["landscape_t12"] = {
        "marker_rule": args.marker,
        "gain_db": [float(value) for value in gains],
        "delay_ms": [round(float(value), 4) for value in delays],
        "score_db": [[float(value) for value in row] for row in values],
        "minimum": {"score_db": float(values[best]), "gain_db": float(gains[best[1]]),
                    "delay_ms": float(delays[best[0]])},
    }
    args.out.write_text(json.dumps(payload, indent=1) + "\n")
    print(f"\n  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
