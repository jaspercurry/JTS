#!/usr/bin/env python3
"""Measured against the builder's out-of-sample prediction, for the identified tunes.

The model was fitted on the earlier rounds, so these tunes are NEW to it: this
is the test of the model, not a restatement of its fit. Prints the behind score
per arm angle, the 1/3-octave bands at 180 deg, the front scores, and the
signed error (measured minus predicted).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from table import BAND_HZ, per_band

POSE = {"+0": "az+0.00_el+0.00_d+1.00", "+20": "az+20.00_el+0.00_d+1.00",
        "-20": "az-20.00_el+0.00_d+1.00"}
ANGLES = ("+0", "+20", "-20")


def find(node: dict, prefix: str) -> str | None:
    return next((key for key in node["candidates"] if key.startswith(prefix)), None)


def score(result: dict, mic: str, angle: str, prefix: str) -> float | None:
    node = result["by_pose"][mic].get(POSE[angle])
    if node is None:
        return None
    key = find(node, prefix)
    return None if key is None else node["candidates"][key]["score_100_350_db"]


def cell(value: float | None, width: int = 7) -> str:
    return f"{'--':>{width}s}" if value is None else f"{value:>+{width}.2f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, nargs="+", required=True)
    parser.add_argument("--predicted", type=Path, required=True)
    parser.add_argument("--tunes", type=Path, required=True,
                        help="JSON {fingerprint-prefix: name}")
    args = parser.parse_args()
    results = [(path.parent.name, json.loads(path.read_text())) for path in args.result]
    predicted = json.loads(args.predicted.read_text())
    tunes = json.loads(args.tunes.read_text())

    print("BEHIND (side mic), score_100_350_db vs rear-muted, by arm angle")
    head = "".join(f"{('r' + str(i + 1)) + ' ' + a:>11s}" for a in ANGLES
                   for i in range(len(results)))
    print(f"  {'tune':<12s}" + "".join(f"{a:>11s}" for a in ANGLES) + f"{'mean':>10s}"
          + f"{'pred mean':>11s}{'err':>8s}")
    errors: list[float] = []
    for prefix, name in tunes.items():
        for label, result in results:
            got = [score(result, "side", a, prefix) for a in ANGLES]
            kept = [v for v in got if v is not None]
            if not kept:
                continue
            mean = float(np.mean(kept))
            want = predicted["behind"].get(name)
            pmean = None if want is None else float(np.mean([want[a] for a in ANGLES]))
            err = None if pmean is None else mean - pmean
            if err is not None and name != "Nm":
                errors.append(err)
            tag = f"{name} [{label}]"
            print(f"  {tag:<12s}" + "".join(cell(v, 11) for v in got)
                  + cell(mean, 10) + cell(pmean, 11) + cell(err, 8))
    print("\n  per-angle error (measured minus predicted)")
    print(f"  {'tune':<12s}" + "".join(f"{a:>11s}" for a in ANGLES))
    for prefix, name in tunes.items():
        want = predicted["behind"].get(name)
        if want is None:
            continue
        for label, result in results:
            got = [score(result, "side", a, prefix) for a in ANGLES]
            if all(v is None for v in got):
                continue
            row = [None if v is None else v - want[a] for v, a in zip(got, ANGLES)]
            print(f"  {name + ' [' + label + ']':<12s}" + "".join(cell(v, 11) for v in row))
    if errors:
        values = np.asarray(errors)
        print(f"\n  MODEL ERROR on these new tunes, 3-angle means: n={values.size}  "
              f"mean {values.mean():+.2f}  mean|err| {np.abs(values).mean():.2f}  "
              f"worst {values[int(np.argmax(np.abs(values)))]:+.2f} dB")

    print("\nPER BAND at 180 deg (arm +0), 1/3-octave mean of the ungated change")
    print(f"  {'tune':<12s}" + "".join(f"{hz:>8.0f}" for hz in BAND_HZ))
    for label, result in results:
        node = result["by_pose"]["side"][POSE["+0"]]
        for prefix, name in tunes.items():
            key = find(node, prefix)
            if key is None:
                continue
            print(f"  {name + ' [' + label + ']':<12s}"
                  + "".join(f"{v:>+8.1f}" for v in per_band(node, key)))
            want = predicted.get("bands180", {}).get(name)
            if want:
                print(f"  {'  predicted':<12s}" + "".join(
                    f"{want[str(int(hz))]:>+8.1f}" if str(int(hz)) in want else f"{'--':>8s}"
                    for hz in BAND_HZ))

    print("\nFRONT (main mic) — guard is >= -2 dB")
    print(f"  {'tune':<12s}" + "".join(f"{a:>11s}" for a in ANGLES) + f"{'mean':>10s}")
    for prefix, name in tunes.items():
        for label, result in results:
            got = [score(result, "main", a, prefix) for a in ANGLES]
            kept = [v for v in got if v is not None]
            if not kept:
                continue
            print(f"  {name + ' [' + label + ']':<12s}" + "".join(cell(v, 11) for v in got)
                  + cell(float(np.mean(kept)), 10))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
