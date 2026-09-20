#!/usr/bin/env python3
"""Complex mean of one measured table across several microphone positions.

A plain complex mean, because a cardioid document has ONE set of filters and
must serve every bearing at once: averaging the complex responses is asking the
fit to cancel the AVERAGE of what the three positions heard. It is not a
substitute for three fits -- the three positions stand different distances from
the cabinet, so their phases differ and the mean is quieter than any of them;
that loss is reported here so it is never read as measured level.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import numpy as np


def read_table(path: Path) -> tuple[np.ndarray, np.ndarray]:
    rows = list(csv.DictReader(path.read_text().splitlines()))
    freqs = np.array([float(row["frequency_hz"]) for row in rows])
    value = 10.0 ** (np.array([float(row["magnitude_db"]) for row in rows]) / 20.0) * np.exp(
        1j * np.radians(np.array([float(row["phase_deg"]) for row in rows])))
    return freqs, value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("inputs", type=Path, nargs="+")
    args = parser.parse_args()
    tables = [read_table(path) for path in args.inputs]
    grid = tables[0][0]
    for freqs, _ in tables[1:]:
        if freqs.shape != grid.shape or not np.allclose(freqs, grid):
            raise SystemExit("mean_tables: the inputs are not on one frequency grid")
    stack = np.stack([value for _, value in tables])
    mean = stack.mean(axis=0)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    lines = ["frequency_hz,magnitude_db,phase_deg"]
    lines += [f"{hz:.6f},{db:.6f},{deg:.6f}" for hz, db, deg in zip(
        grid, 20.0 * np.log10(np.maximum(np.abs(mean), 1e-12)), np.degrees(np.angle(mean)))]
    args.out.write_text("\n".join(lines) + "\n")
    band = (grid >= 100.0) & (grid < 350.0)
    incoherent = 20.0 * np.log10(
        np.abs(mean[band]).mean() / np.abs(stack[:, band]).mean())
    print(f"wrote {args.out} ({grid.size} rows from {len(tables)} positions); "
          f"the complex mean sits {incoherent:+.2f} dB under the mean MAGNITUDE "
          f"over 100-350 Hz -- that gap is how much the positions disagree in phase")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
