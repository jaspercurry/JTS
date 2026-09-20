"""The three BA graphs plus ba-numbers.json (#5405 before/after).

usage: ba_graphs.py '<{fp8: tag}>' <res-ba2.json> <res-ba1.json> [res-ba3.json]
"""
from __future__ import annotations

import json
import sys

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker

from ba_lib import CHECK_POSES, POSE0, POSE_M20, POSE_P20, POSES3, SP, curve, load, smooth

STYLE = {
    "C0": ("C0  linearization only, rear muted", "#777777", "--", 1.6),
    "B0": ("B0  fair cardioid off (front matched)", "#1f77b4", "-", 1.8),
    "A0": ("A0  cardioid (agg-1)", "#d62728", "-", 2.4),
    "A1": ("A1  cardioid + room", "#d62728", ":", 2.4),
    "B1": ("B1  cardioid off + room", "#1f77b4", ":", 1.8),
}
REAR_LABEL = {POSE_P20: "160 deg", POSE0: "180 deg", POSE_M20: "200 deg"}
SUB = "jts3 mid-room on a mini fridge; UMIK-2 0.61 m in front, Dayton iMM-6C 0.61 m behind"


def front_label(pose):
    return pose.split("_")[0].replace("az", "") + " deg"


def log_axis(ax, lo, hi, ticks):
    ax.set_xscale("log")
    ax.set_xlim(lo, hi)
    ax.set_xticks(ticks)
    ax.get_xaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter())
    ax.get_xaxis().set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.tick_params(labelsize=8)
    ax.grid(True, which="both", alpha=0.3)


def third_octave(freqs, mags, centre):
    sel = (freqs >= centre / 2 ** (1 / 6)) & (freqs < centre * 2 ** (1 / 6))
    if not sel.any():
        return float(np.interp(centre, freqs, mags))
    return float(10 * np.log10(np.mean(10 ** (mags[sel] / 10))))


def align(freqs, mags, ref, lo=500.0, hi=2000.0):
    sel = (freqs >= lo) & (freqs <= hi)
    return mags - float(np.mean((mags - ref)[sel]))


def get(results, tag, fps, mic, pose):
    for res in results:
        try:
            return curve(res, mic, pose, fps[tag])
        except SystemExit:
            continue
    return None


def mean_over(results, tag, fps, mic, poses):
    curves, freqs = [], None
    for pose in poses:
        got = get(results, tag, fps, mic, pose)
        if got is None:
            continue
        freqs, mags = got
        curves.append(mags)
    if not curves:
        return None
    return freqs, np.mean(curves, axis=0)


def rms_to_target(freqs, curve_db, tfreqs, target, lo=40.0, hi=500.0):
    dev = curve_db - np.interp(freqs, tfreqs, target)
    sel = (freqs >= lo) & (freqs <= hi)
    return float(np.sqrt(np.mean(dev[sel] ** 2)))


def g1(results, fps, order, numbers):
    fig, axes = plt.subplots(2, 1, figsize=(13, 9), sharex=True,
                             gridspec_kw={"height_ratios": [2, 1]})
    ref_f, ref_m = get(results, "C0", fps, "main", POSE0)
    ref_s = smooth(ref_f, ref_m, 1 / 6.0)
    for tag in order:
        freqs, mags = get(results, tag, fps, "main", POSE0)
        sm = align(freqs, smooth(freqs, mags, 1 / 6.0), ref_s)
        label, colour, ls, lw = STYLE[tag]
        axes[0].plot(freqs, sm, color=colour, ls=ls, lw=lw, label=label)
        axes[1].plot(freqs, sm - ref_s, color=colour, ls=ls, lw=lw)
        numbers["front_pose0_third_octave_vs_C0"][tag] = {
            str(c): round(third_octave(freqs, sm - ref_s, c), 2)
            for c in (31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500,
                      630, 800, 1000, 2000, 4000, 8000, 16000)}
    ticks = [20, 30, 50, 80, 125, 200, 315, 500, 800, 1250, 2000, 3150, 5000, 8000, 12500, 20000]
    for ax in axes:
        log_axis(ax, 20, 20000, ticks)
    axes[1].axhline(0, color="#777777", ls="--", lw=1.2)
    axes[0].set_ylabel("level, dB (aligned 500 Hz - 2 kHz)", fontsize=9)
    axes[1].set_ylabel("minus C0, dB", fontsize=9)
    axes[1].set_xlabel("Hz", fontsize=9)
    axes[0].legend(fontsize=9, loc="lower center", ncol=2)
    fig.suptitle("Front response 20 Hz - 20 kHz, one position (0 deg), 1/6-octave smoothed, "
                 "all room sound in (ungated)\n" + SUB, fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    out = SP / "graphs" / "ba-front-20-20k.png"
    fig.savefig(out, dpi=110)
    plt.close(fig)
    print(out)


def g2(results, fps, room, numbers, check_poses):
    groups = [("fit poses (0, +20, -20)", POSES3), ("check poses (-30, -10, +10, +30)", check_poses)]
    fig, axes = plt.subplots(2, 2, figsize=(15, 9), sharex=True)
    for col, (gname, poses) in enumerate(groups):
        for row, (base, treated) in enumerate((("A0", "A1"), ("B0", "B1"))):
            ax = axes[row][col]
            tfreqs = np.asarray(room["_curves"][base]["freqs_hz"], float)
            target = np.asarray(room["_curves"][base]["target_db"], float)
            got = mean_over(results, base, fps, "main", poses)
            if got is None:
                ax.set_visible(False)
                continue
            freqs, base_mean = got
            base_s = smooth(freqs, base_mean, 1 / 6.0)
            ax.plot(tfreqs, target, color="#000000", ls="-", lw=1.2, label="target (own broad trend)")
            for tag in (base, treated):
                freqs, mean = mean_over(results, tag, fps, "main", poses)
                raw = smooth(freqs, mean, 1 / 6.0)
                sm = align(freqs, raw, base_s)
                if tag != base:
                    numbers.setdefault("level_cost_db", {})[tag] = round(float(np.mean(
                        (raw - base_s)[(freqs >= 500) & (freqs <= 2000)])), 2)
                value = rms_to_target(freqs, sm, tfreqs, target)
                numbers["deviation_rms_40_500_db"].setdefault(gname, {})[tag] = round(value, 2)
                # 50-500 Hz too: the 40 Hz band is the cabinet's own roll-off,
                # ~8 dB under the straight-line target and outside the +4 dB cap.
                numbers["deviation_rms_50_500_db"].setdefault(gname, {})[tag] = round(
                    rms_to_target(freqs, sm, tfreqs, target, lo=50.0), 2)
                label, colour, ls, lw = STYLE[tag]
                ax.plot(freqs, sm, color=colour, ls=ls, lw=lw, label=f"{tag}  RMS {value:.2f} dB")
            log_axis(ax, 30, 600, [30, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 600])
            ax.axvspan(40, 500, color="#ffe9a8", alpha=0.3, lw=0)
            ax.set_ylim(-40, -12)
            ax.legend(fontsize=8, loc="lower left")
            ax.set_title(f"{'cardioid ON' if row == 0 else 'cardioid OFF'} - {gname}", fontsize=10)
            if col == 0:
                ax.set_ylabel("level, dB (1/6-octave, mean of the poses)", fontsize=9)
            if row == 1:
                ax.set_xlabel("Hz", fontsize=9)
    for key in ("deviation_rms_40_500_db", "deviation_rms_50_500_db"):
        for gname, _ in groups:
            table = numbers[key].get(gname, {})
            for base, treated in (("A0", "A1"), ("B0", "B1")):
                if base in table and treated in table:
                    numbers["improvement_db"].setdefault(key, {}).setdefault(gname, {})[
                        f"{base}->{treated}"] = round(table[base] - table[treated], 2)
    fig.suptitle("Room correction, 30-600 Hz. RMS = deviation from that tune's own straight-line "
                 "target, 40-500 Hz (yellow band).\nLEFT: the three poses the fit was made on. "
                 "RIGHT: four bearings the fit never saw. Each pair is level-aligned over "
                 "500 Hz - 2 kHz, so the RMS reads shape, not the headroom the boosts cost.\n" + SUB,
                 fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    out = SP / "graphs" / "ba-room-pair.png"
    fig.savefig(out, dpi=110)
    plt.close(fig)
    print(out)


def g3(results, fps, order, numbers, side_poses, side_note):
    fig, axes = plt.subplots(1, 2, figsize=(16, 6))
    ax = axes[0]
    behind = {}
    for tag in order:
        got = mean_over(results, tag, fps, "side", side_poses)
        if got is None:
            continue
        freqs, mean = got
        behind[tag] = (freqs, mean)
        label, colour, ls, lw = STYLE[tag]
        ax.plot(freqs, smooth(freqs, mean, 1 / 6.0), color=colour, ls=ls, lw=lw, label=label)
    log_axis(ax, 20, 1000, [20, 30, 50, 80, 125, 200, 315, 500, 800, 1000])
    ax.set_ylabel("level, dB", fontsize=9)
    ax.set_xlabel("Hz", fontsize=9)
    ax.set_title(f"BEHIND the box, {side_note} (1/6-octave, ungated)", fontsize=10)
    ax.legend(fontsize=8, loc="lower left")

    ax = axes[1]
    centres = [50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500]
    front = {tag: mean_over(results, tag, fps, "main", POSES3) for tag in order}
    for tag, against in (("A0", "B0"), ("A1", "B1")):
        values = []
        for c in centres:
            df = third_octave(*front[tag], c) - third_octave(*front[against], c)
            db = third_octave(*behind[tag], c) - third_octave(*behind[against], c)
            values.append(df - db)
        numbers["front_to_back_gain_db"][f"{tag}_over_{against}"] = {
            str(c): round(v, 2) for c, v in zip(centres, values)}
        ax.plot(centres, values, marker="o", lw=2.0, color=STYLE[tag][1], ls=STYLE[tag][2],
                label=f"{tag} over {against}")
    log_axis(ax, 45, 560, centres)
    ax.axhline(0, color="#777777", ls="--", lw=1.2)
    ax.set_ylabel("front-to-back gain over the matched no-rear tune, dB", fontsize=9)
    ax.set_xlabel("third-octave centre, Hz", fontsize=9)
    ax.set_title("What the rear woofer buys: (front change) - (behind change), per third octave",
                 fontsize=10)
    ax.legend(fontsize=9)
    fig.suptitle("Behind the cabinet, and the directivity the cardioid buys. "
                 "Front = mean of the three fit poses; all ungated.\n" + SUB, fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    out = SP / "graphs" / "ba-behind.png"
    fig.savefig(out, dpi=110)
    plt.close(fig)
    print(out)


def main():
    fps = json.loads(sys.argv[1])
    results = [load(p) for p in sys.argv[2:]]
    room = load(SP / "search" / "BA" / "room-fits.json")
    order = ["C0", "B0", "A0", "A1", "B1"]
    have_check = [p for p in CHECK_POSES
                  if any(p in r["by_pose"]["main"] for r in results)]
    side_mirror = [p for p in CHECK_POSES
                   if any(p in r["by_pose"]["side"] for r in results)]
    if len(side_mirror) == len(CHECK_POSES):
        side_poses = (POSE_P20, POSE_M20, *side_mirror)
        side_note = "mean of 150/170/190/210 deg (check bearings) and 160/200 deg"
    else:
        side_poses = (POSE_P20, POSE_M20)
        side_note = "mean of 160 and 200 deg"
    numbers = {"fingerprints": fps, "smoothing": "1/6 octave for display",
               "check_poses": have_check, "side_poses": list(side_poses),
               "front_pose0_third_octave_vs_C0": {},
               "deviation_rms_40_500_db": {}, "deviation_rms_50_500_db": {}, "improvement_db": {},
               "front_to_back_gain_db": {},
               "room_fits": {k: v for k, v in room.items() if k != "_curves"}}
    g1(results, fps, order, numbers)
    g2(results, fps, room, numbers, have_check)
    g3(results, fps, order, numbers, side_poses, side_note)
    (SP / "graphs" / "ba-numbers.json").write_text(json.dumps(numbers, indent=1) + "\n")
    print(SP / "graphs" / "ba-numbers.json")
    print(json.dumps(numbers["deviation_rms_40_500_db"], indent=1))
    print(json.dumps(numbers["deviation_rms_50_500_db"], indent=1))
    print(json.dumps(numbers["improvement_db"], indent=1))


if __name__ == "__main__":
    main()
