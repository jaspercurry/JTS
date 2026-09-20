"""Why the back of the room sounds bassier: the same tune at both microphones.

Each curve is referenced to ITS OWN mic's 500 Hz - 2 kHz level, so the two mics
become comparable as tone even though their absolute levels are not (different
capsules, different distances, different calibration files).
"""
from __future__ import annotations

import json
import sys

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from ba_lib import POSE0, SP, curve, load, smooth
from wall_graphs import TICKS_FULL, plain_axis
from wall_numbers import normalise

BANDS = {"40-80 Hz": (40.0, 80.0), "80-160 Hz": (80.0, 160.0)}
TUNES = ["C0", "B0", "A0"]
MIC_STYLE = {"main": ("FRONT  UMIK-2, 0.81 m", "-", 2.6), "side": ("SEAT  Dayton, ~2 m", ":", 2.6)}
COLOUR = {"C0": "#777777", "B0": "#1f77b4", "A0": "#d62728"}


def band_level(freqs, norm, lo, hi):
    sel = (freqs >= lo) & (freqs <= hi)
    return float(10 * np.log10(np.mean(10 ** (norm[sel] / 10))))


def main():
    result = load(sys.argv[1])
    labels = json.loads(sys.argv[2])
    table = {}
    fig, ax = plt.subplots(figsize=(13, 7))
    for tag in TUNES:
        for mic, (mic_name, ls, lw) in MIC_STYLE.items():
            freqs, mags = curve(result, mic, POSE0, labels[tag])
            norm = normalise(freqs, smooth(freqs, mags, 1 / 6.0))
            for band, (lo, hi) in BANDS.items():
                table.setdefault(tag, {}).setdefault(band, {})[mic] = round(
                    band_level(freqs, norm, lo, hi), 2)
            ax.plot(freqs, norm, color=COLOUR[tag], ls=ls, lw=lw,
                    label=f"{tag}  {mic_name}")
    for tag in TUNES:
        for band in BANDS:
            row = table[tag][band]
            row["seat_minus_front_db"] = round(row["side"] - row["main"], 2)
    plain_axis(ax, 20, 20000, TICKS_FULL)
    for lo, hi in BANDS.values():
        ax.axvspan(lo, hi, color="#ffe9a8", alpha=0.25, lw=0)
    ax.axhline(0, color="#777777", ls="--", lw=1.0)
    ax.set_ylabel("dB, 0 = that MIC's own mean over 500 Hz - 2 kHz", fontsize=9)
    ax.set_xlabel("Hz", fontsize=9)
    ax.legend(fontsize=9, loc="lower center", ncol=3)
    ax.set_ylim(-25, 16)
    rows = ["seat minus front, dB", "           40-80   80-160"]
    for tag in TUNES:
        a = table[tag]["40-80 Hz"]["seat_minus_front_db"]
        b = table[tag]["80-160 Hz"]["seat_minus_front_db"]
        rows.append(f"{tag:4s}     {a:+6.1f}   {b:+6.1f}")
    ax.text(0.985, 0.97, chr(10).join(rows), transform=ax.transAxes, fontsize=9,
            family="monospace", va="top", ha="right",
            bbox={"boxstyle": "round", "fc": "white", "ec": "#999999", "alpha": 0.9})
    ax.set_title("Solid = front mic at 0.81 m,  dotted = seat mic at ~2 m", fontsize=9)
    fig.suptitle("Is there more bass further back? The same tune at both microphones.\n"
                 "Each curve is referenced to ITS OWN mic's 500 Hz - 2 kHz level, so the two "
                 "are comparable as TONE, never as loudness. Shaded: 40-80 and 80-160 Hz.",
                 fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    out = SP / "graphs" / "wall1-bass-front-vs-seat.png"
    fig.savefig(out, dpi=110)
    plt.close(fig)
    print(out)
    for tag in TUNES:
        for band in BANDS:
            row = table[tag][band]
            print(f"  {tag:3s} {band:10s} front {row['main']:+6.2f}  seat {row['side']:+6.2f}  "
                  f"seat-front {row['seat_minus_front_db']:+6.2f} dB")
    path = SP / "graphs" / "wall1-numbers.json"
    payload = json.loads(path.read_text()) if path.exists() else {}
    payload["bass_front_vs_seat"] = table
    path.write_text(json.dumps(payload, indent=1) + "\n")
    print(path)


if __name__ == "__main__":
    main()
