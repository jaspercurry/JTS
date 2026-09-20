"""WALL1 graphs. Owner's style rules: every curve normalised around 0 dB, and
plain numbers on the frequency axis (no exponent ticks).
"""
from __future__ import annotations

import json
import sys

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker

from ba_lib import POSE0, SP, curve, load, smooth
from wall_numbers import WALL_BAND, normalise, trend, wall_hole

WALL_MARK_HZ = 171.0
STYLE = {
    "C0": ("C0  rear simply off", "#777777", "--", 2.0),
    "B0": ("B0  fair off (front matched)", "#1f77b4", "-", 2.2),
    "A0": ("A0  agg-1 cardioid", "#d62728", "-", 2.8),
    "N1": ("N1  first cardioid seed", "#2ca02c", "-", 1.1),
    "identC": ("ident-C  measured-rear solution", "#ff7f0e", "-", 1.1),
    "wall170": ("wall170-1  fitted for the wall band", "#9467bd", "-", 1.1),
}
ORDER = ["C0", "B0", "A0", "N1", "identC", "wall170"]
TICKS_FULL = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]
TICKS_ZOOM = [50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500]
SUB = ("jts3 cabinet back ~0.2 m from a wall.  SEAT = Dayton iMM-6C at the listening position, "
       "~2 m, ~20 deg off axis.\nFRONT = UMIK-2 on the arm, 0.81 m, pose 0.  All ungated, "
       "round wall1c.")


def plain_axis(ax, lo, hi, ticks):
    ax.set_xscale("log")
    ax.set_xlim(lo, hi)
    ax.set_xticks(ticks)
    ax.get_xaxis().set_major_formatter(matplotlib.ticker.FuncFormatter(
        lambda v, _pos: f"{v / 1000:g}k" if v >= 1000 else f"{v:g}"))
    ax.get_xaxis().set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.tick_params(labelsize=8)
    ax.grid(True, which="both", alpha=0.3)


def traces(result, mic, labels, octaves):
    out = {}
    for tag, fp8 in labels.items():
        freqs, mags = curve(result, mic, POSE0, fp8)
        out[tag] = (freqs, normalise(freqs, smooth(freqs, mags, octaves)))
    return out


def two_panel(result, mic, labels, title, path):
    fig, axes = plt.subplots(2, 1, figsize=(13, 9), sharex=True,
                             gridspec_kw={"height_ratios": [2, 1]})
    rows = traces(result, mic, labels, 1 / 6.0)
    ref_f, ref = rows["B0"]
    for tag in ORDER:
        if tag not in rows:
            continue
        freqs, norm = rows[tag]
        label, colour, ls, lw = STYLE[tag]
        axes[0].plot(freqs, norm, color=colour, ls=ls, lw=lw, label=label)
        axes[1].plot(freqs, norm - ref, color=colour, ls=ls, lw=lw)
    for ax in axes:
        plain_axis(ax, 20, 20000, TICKS_FULL)
        ax.axvline(WALL_MARK_HZ, color="#8c564b", ls=":", lw=1.4)
    axes[1].axhline(0, color="#777777", ls="--", lw=1.2)
    axes[0].set_ylabel("dB, 0 = own mean over 500 Hz - 2 kHz", fontsize=9)
    axes[0].set_ylim(-32, 20)
    axes[1].set_ylabel("minus B0 (fair off), dB", fontsize=9)
    axes[1].set_ylim(-14, 14)
    axes[1].set_xlabel("Hz", fontsize=9)
    axes[0].legend(fontsize=9, loc="lower center", ncol=3)
    axes[0].annotate("171 Hz\nwall band", xy=(WALL_MARK_HZ, 0.02), xycoords=("data", "axes fraction"),
                     fontsize=8, color="#8c564b", ha="center")
    fig.suptitle(title + "\n0 dB = each curve's OWN mean over 500 Hz - 2 kHz: the panel compares "
                 "SHAPE, not loudness.\n" + SUB, fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    fig.savefig(path, dpi=110)
    plt.close(fig)
    print(path)


def zoom(result, mic, labels, title, path):
    fig, ax = plt.subplots(figsize=(13, 8))
    rows = traces(result, mic, labels, 1 / 12.0)
    lines = []
    for tag in ORDER:
        if tag not in rows:
            continue
        freqs, norm = rows[tag]
        raw_f, raw = curve(result, mic, POSE0, labels[tag])
        depth, at_hz = wall_hole(raw_f, raw)
        label, colour, ls, lw = STYLE[tag]
        ax.plot(freqs, norm, color=colour, ls=ls, lw=lw,
                label=f"{label}   hole {depth:+.1f} dB @ {at_hz:.0f} Hz")
        lines.append(tag)
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
                 "1-octave trend.  1/12-octave smoothing.\n" + SUB, fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    fig.savefig(path, dpi=110)
    plt.close(fig)
    print(path)


def front_pair(result, labels, path):
    fig, axes = plt.subplots(2, 1, figsize=(13, 10))
    rows6 = traces(result, "main", labels, 1 / 6.0)
    rows12 = traces(result, "main", labels, 1 / 12.0)
    for tag in ORDER:
        if tag not in rows6:
            continue
        label, colour, ls, lw = STYLE[tag]
        axes[0].plot(*rows6[tag], color=colour, ls=ls, lw=lw, label=label)
        raw_f, raw = curve(result, "main", POSE0, labels[tag])
        depth, at_hz = wall_hole(raw_f, raw)
        axes[1].plot(*rows12[tag], color=colour, ls=ls, lw=lw,
                     label=f"{tag}  hole {depth:+.1f} dB @ {at_hz:.0f} Hz")
    plain_axis(axes[0], 20, 20000, TICKS_FULL)
    plain_axis(axes[1], 50, 500, TICKS_ZOOM)
    axes[1].set_ylim(-28, 18)
    for ax in axes:
        ax.axvline(WALL_MARK_HZ, color="#8c564b", ls=":", lw=1.4)
        ax.set_ylabel("dB, 0 = own mean 500 Hz - 2 kHz", fontsize=9)
        ax.set_xlabel("Hz", fontsize=9)
    axes[1].axvspan(*WALL_BAND, color="#ffe9a8", alpha=0.3, lw=0)
    axes[0].legend(fontsize=8, loc="lower center", ncol=3)
    axes[1].legend(fontsize=8, loc="lower left")
    axes[0].set_title("20 Hz - 20 kHz, 1/6-octave", fontsize=10)
    axes[1].set_title("50 - 500 Hz, 1/12-octave, wall band shaded", fontsize=10)
    fig.suptitle("FRONT mic, UMIK-2 on the arm at 0.81 m, pose 0.\n0 dB = each curve's own mean "
                 "over 500 Hz - 2 kHz.\n" + SUB, fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    fig.savefig(path, dpi=110)
    plt.close(fig)
    print(path)


def main():
    result = load(sys.argv[1])
    labels = json.loads(sys.argv[2])
    two_panel(result, "side", labels,
              "SEAT microphone, 20 Hz - 20 kHz, 1/6-octave",
              SP / "graphs" / "wall1-seat-20-20k.png")
    zoom(result, "side", labels, "SEAT microphone, 50 - 500 Hz: the wall band",
         SP / "graphs" / "wall1-seat-zoom.png")
    front_pair(result, labels, SP / "graphs" / "wall1-front.png")


if __name__ == "__main__":
    main()
