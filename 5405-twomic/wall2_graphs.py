"""WALL2 graphs. Owner's style: every curve around 0 dB, plain Hz numbers."""
from __future__ import annotations

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from ba_lib import POSE0, SP, curve, load, smooth
from wall_graphs import TICKS_FULL, TICKS_ZOOM, WALL_MARK_HZ, plain_axis
from wall_numbers import WALL_BAND, normalise, wall_hole
from wall2_tables import LABELS

STYLE = {
    "C0": ("C0  rear simply off", "#777777", "--", 2.0),
    "B0": ("B0  fair off (front matched)", "#1f77b4", "-", 2.2),
    "A0": ("A0  agg-1 cardioid", "#d62728", "-", 2.2),
    "seat-1": ("seat-1  fitted for the seat (risky)", "#2ca02c", "-", 3.0),
    "seat-2": ("seat-2  agg-1 with the low bell moved", "#9467bd", "-", 2.0),
    "wall170": ("wall170-1  fitted for the wall band", "#ff7f0e", "-", 1.1),
}
ORDER = ["C0", "B0", "A0", "seat-1", "seat-2", "wall170"]
SUB = ("jts3 cabinet back ~0.2 m from a wall.  SEAT = Dayton iMM-6C at the listening position, "
       "~2 m, ~20 deg off axis.\nFRONT = UMIK-2 on the arm, 0.81 m, pose 0.  All ungated, "
       "round wall2.")


def rows(result, mic, octaves):
    return {tag: (lambda fm: (fm[0], normalise(fm[0], smooth(fm[0], fm[1], octaves))))(
        curve(result, mic, POSE0, fp8)) for tag, fp8 in LABELS.items()}


def zoom(result, mic, title, path):
    fig, ax = plt.subplots(figsize=(13, 8))
    data = rows(result, mic, 1 / 12.0)
    for tag in ORDER:
        f, n = data[tag]
        rf, rm = curve(result, mic, POSE0, LABELS[tag])
        depth, at_hz = wall_hole(rf, rm)
        label, colour, ls, lw = STYLE[tag]
        ax.plot(f, n, color=colour, ls=ls, lw=lw,
                label=f"{label}   hole {depth:+.1f} dB @ {at_hz:.0f} Hz")
    plain_axis(ax, 50, 500, TICKS_ZOOM)
    ax.set_ylim(-28, 18)
    ax.axvline(WALL_MARK_HZ, color="#8c564b", ls=":", lw=1.6)
    ax.axvspan(*WALL_BAND, color="#ffe9a8", alpha=0.3, lw=0)
    ax.annotate("171 Hz", xy=(WALL_MARK_HZ, 0.02), xycoords=("data", "axes fraction"),
                fontsize=9, color="#8c564b", ha="center")
    ax.set_ylabel("dB, 0 = own mean over 500 Hz - 2 kHz", fontsize=9)
    ax.set_xlabel("Hz", fontsize=9)
    ax.legend(fontsize=9, loc="lower left")
    fig.suptitle(title + "\nhole = deepest dip inside 120-260 Hz (yellow) against that curve's own "
                 "1-octave trend.  1/12-octave.\n" + SUB, fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    fig.savefig(path, dpi=110)
    plt.close(fig)
    print(path)


def full(result, mic, title, path):
    fig, axes = plt.subplots(2, 1, figsize=(13, 9), sharex=True,
                             gridspec_kw={"height_ratios": [2, 1]})
    data = rows(result, mic, 1 / 6.0)
    ref_f, ref = data["B0"]
    for tag in ORDER:
        f, n = data[tag]
        label, colour, ls, lw = STYLE[tag]
        axes[0].plot(f, n, color=colour, ls=ls, lw=lw, label=label)
        axes[1].plot(f, n - ref, color=colour, ls=ls, lw=lw)
    for ax in axes:
        plain_axis(ax, 20, 20000, TICKS_FULL)
        ax.axvline(WALL_MARK_HZ, color="#8c564b", ls=":", lw=1.4)
    axes[1].axhline(0, color="#777777", ls="--", lw=1.2)
    axes[0].set_ylim(-32, 20)
    axes[1].set_ylim(-12, 12)
    axes[0].set_ylabel("dB, 0 = own mean over 500 Hz - 2 kHz", fontsize=9)
    axes[1].set_ylabel("minus B0 (fair off), dB", fontsize=9)
    axes[1].set_xlabel("Hz", fontsize=9)
    axes[0].legend(fontsize=8, loc="lower center", ncol=3)
    fig.suptitle(title + "\n0 dB = each curve's OWN mean over 500 Hz - 2 kHz: the panel compares "
                 "SHAPE, not loudness.\n" + SUB, fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    fig.savefig(path, dpi=110)
    plt.close(fig)
    print(path)


def main():
    result = load(SP / "search" / "BA" / "res-wall2.json")
    zoom(result, "side", "SEAT microphone, 50 - 500 Hz: the wall band",
         SP / "graphs" / "wall2-seat-zoom.png")
    full(result, "side", "SEAT microphone, 20 Hz - 20 kHz, 1/6-octave",
         SP / "graphs" / "wall2-seat-20-20k.png")
    full(result, "main", "FRONT microphone (UMIK-2, 0.81 m), 20 Hz - 20 kHz, 1/6-octave",
         SP / "graphs" / "wall2-front.png")


if __name__ == "__main__":
    main()
