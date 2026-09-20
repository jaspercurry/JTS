#!/usr/bin/env python3
"""The three early/late figures for round H4, plus the JSON behind them."""
from __future__ import annotations

import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt          # noqa: E402
import matplotlib.ticker                 # noqa: E402
import numpy as np                       # noqa: E402

import h4core as hc                      # noqa: E402
from h4load import rows                  # noqa: E402

OUT = hc.SP / "graphs"
DPI = 110
#: The decayed tail, used as the floor. The impulse is CIRCULAR over 682.7 ms,
#: so times run -341..+341 ms about the arrival: a window reaching past +341 ms
#: is silently clipped. 260..340 ms is the latest honest floor window that still
#: clears the +200 ms end of the late window.
TAIL_MS = (260.0, 340.0)
SHOW_MS = 120.0
ORDER = ("0caaa048", "5e9afae3", "711b458f", "2fc52a19", "654e057b")

nodes = hc.aligned_transfers(rows())
FRONT = [p for p in hc.POSES]
BEHIND = [p for p in hc.POSES if "az+0.00" not in p]


def marker(mic, pose):
    return nodes[(mic, pose)]["marker"]


def impulse(mic, pose, fp, band):
    node = nodes[(mic, pose)]
    return hc.rolled_impulse(node["tunes"][fp], band, node["marker"])


def present(mic, poses):
    """Tunes that survived the aligner in EVERY pose of this mic."""
    return [fp for fp in ORDER if all(fp in nodes[(mic, p)]["tunes"] for p in poses)]


# ---------------------------------------------------------------- figure 1
def energy_time(mic, poses):
    """Pose-mean envelope energy (dB re the OFF peak) and cumulative fraction."""
    span = int(SHOW_MS * 1e-3 * hc.FS)
    tunes = present(mic, poses)
    envelopes = {fp: [] for fp in tunes}
    cumulative = {fp: [] for fp in tunes}
    for pose in poses:
        reference = hc.envelope_db(impulse(mic, pose, hc.MUTED, hc.BAND_100_350))
        top = float(np.max(reference))
        for fp in tunes:
            ir = impulse(mic, pose, fp, hc.BAND_100_350)
            envelopes[fp].append(hc.envelope_db(ir)[:span] / top)
            energy = ir[:int(0.200 * hc.FS)] ** 2
            cumulative[fp].append(np.cumsum(energy)[:span] / max(float(np.sum(energy)), 1e-30))
    time = np.arange(span) / hc.FS * 1e3
    return (time,
            {fp: 10 * np.log10(np.maximum(np.mean(envelopes[fp], axis=0), 1e-12)) for fp in tunes},
            {fp: 100.0 * np.mean(cumulative[fp], axis=0) for fp in tunes})


def figure_one():
    fig, axes = plt.subplots(2, 2, figsize=(15, 9), sharex=True)
    held = {}
    for col, (mic, poses, where) in enumerate(
            (("main", FRONT, "IN FRONT (UMIK-2, 0.61 m, mean of the 3 arm poses)"),
             ("side", BEHIND, "BEHIND (Dayton, 0.61 m, mean of 160° and 200°)"))):
        time, envelopes, cumulative = energy_time(mic, poses)
        held[mic] = {"time_ms": time[::48].tolist(),
                     "envelope_db": {hc.TUNES[k][0]: v[::48].tolist() for k, v in envelopes.items()},
                     "cumulative_pct": {hc.TUNES[k][0]: v[::48].tolist()
                                        for k, v in cumulative.items()}}
        for row, (data, label, limits) in enumerate((
                (envelopes, "envelope energy, dB re the OFF direct peak", (-38, 4)),
                (cumulative, "% of the energy in the first 200 ms", (0, 102)))):
            ax = axes[row][col]
            for fp in ORDER:
                if fp not in data:
                    continue
                name, colour, style, width = hc.TUNES[fp]
                ax.plot(time, data[fp], color=colour, ls=style, lw=width, label=name)
            for mark in (10.0, 20.0):
                ax.axvline(mark, color="#444444", ls=":", lw=1.1)
                ax.text(mark, limits[1], f" {mark:.0f} ms", fontsize=8, va="top", color="#444444")
            ax.set_xlim(0, SHOW_MS)
            ax.set_ylim(*limits)
            ax.grid(True, alpha=0.3)
            ax.set_ylabel(label, fontsize=9)
            if row == 0:
                ax.set_title(where, fontsize=10)
            else:
                ax.set_xlabel("ms after the direct arrival", fontsize=9)
    axes[0][0].legend(fontsize=9, loc="upper right")
    axes[0][1].text(SHOW_MS * 0.30, 1.5,
                    "behind the box there is no single sharp arrival: the two poses peak at\n"
                    "different instants, so their average starts below 0 dB. That is the room,\n"
                    "not a fault of the tune.", fontsize=8, color="#444444", va="top")
    fig.suptitle(
        "How the 100–350 Hz sound arrives: direct first, then the room — round 8ae2ac84b867 (H4)\n"
        "Hilbert envelope of the zero-phase band-passed impulse, smoothed 3 ms. t=0 is the direct arrival "
        "(1–4 kHz envelope peak of the rear-muted take, shared by every tune of that pose).\n"
        "A 100–350 Hz band-pass rings for about 4 ms, so nothing here resolves time finer than that. "
        "Curves are normalised per pose to the rear-muted direct peak, then averaged.",
        fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.90))
    path = OUT / "h4-energy-time.png"
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    print(path)
    return held


# ---------------------------------------------------------------- figure 2
def early_late(mic, poses, bands):
    """{tune: {band: (mean ratio dB, change vs OFF dB)}}, dB meaned over poses."""
    tunes = present(mic, poses)
    out: dict = {fp: {} for fp in tunes}
    for name, band in bands:
        per_pose = {fp: [] for fp in tunes}
        for pose in poses:
            for fp in tunes:
                ir = impulse(mic, pose, fp, band)
                early = hc.window_energy(ir, *hc.EARLY_MS)
                late = hc.window_energy(ir, *hc.LATE_MS)
                per_pose[fp].append(10 * np.log10(max(early, 1e-30) / max(late, 1e-30)))
        reference = float(np.mean(per_pose[hc.MUTED]))
        for fp in tunes:
            value = float(np.mean(per_pose[fp]))
            out[fp][name] = (value, value - reference,
                             float(np.std(per_pose[fp], ddof=0)))
    return out


def figure_two(repeat_db):
    fig, axes = plt.subplots(1, 2, figsize=(16, 7.4), sharey=True)
    held = {}
    names = [n for n, _ in hc.OCTAVES]
    panels = [("main", FRONT, "IN FRONT — mean of the 3 arm poses"),
              ("side", BEHIND, "BEHIND — mean of 160° and 200°")]
    tables = {mic: early_late(mic, poses, hc.OCTAVES) for mic, poses, _ in panels}
    values = [v[0] for table in tables.values() for row in table.values() for v in row.values()]
    low, top = min(values), max(values)
    limits = (low - 1.2, top + 5.2)
    for ax, (mic, _poses, where) in zip(axes, panels):
        table = tables[mic]
        held[mic] = {hc.TUNES[fp][0]: {n: {"early_late_db": v[0], "change_vs_off_db": v[1],
                                           "pose_spread_db": v[2]}
                                       for n, v in table[fp].items()} for fp in table}
        tunes = [fp for fp in ORDER if fp in table]
        width = 0.8 / len(tunes)
        base = np.arange(len(names))
        for index, fp in enumerate(tunes):
            label, colour, _style, _lw = hc.TUNES[fp]
            offset = base + (index - (len(tunes) - 1) / 2) * width
            heights = [table[fp][n][0] for n in names]
            ax.bar(offset, heights, width * 0.92, color=colour, label=label,
                   edgecolor="white", linewidth=0.6,
                   yerr=[table[fp][n][2] for n in names],
                   error_kw={"ecolor": "#333333", "elinewidth": 0.9, "capsize": 2})
            for x, name in zip(offset, names):
                text = "OFF ref" if fp == hc.MUTED else f"{table[fp][name][1]:+.1f}"
                ax.text(x, top + 0.9, text, rotation=90, fontsize=7.6, ha="center",
                        va="bottom", color="#333333" if fp == hc.MUTED else colour)
        ax.set_xticks(base)
        ax.set_xticklabels([f"{n}\n(rings ~{hc.RING_MS[n]:.0f} ms)" for n in names], fontsize=9)
        ax.axhline(0, color="#777777", lw=1.0)
        ax.grid(True, axis="y", alpha=0.3)
        ax.set_title(where, fontsize=10)
        ax.set_ylim(*limits)
    axes[0].set_ylabel("early-to-late energy ratio, dB   (higher = more direct, less room)", fontsize=9)
    axes[0].legend(fontsize=9, loc="lower left")
    fig.text(0.5, 0.015,
             "Black whiskers = spread between arm poses; that is real room variation, not error. "
             f"Back-to-back repeat error of this number: {repeat_db:.2f} dB worst, measured on the "
             "four tunes this round captured twice.",
             fontsize=8.5, color="#333333", ha="center")
    fig.suptitle(
        "Early versus late energy, cardioid OFF (rear woofer muted) against each tune — round 8ae2ac84b867 (H4)\n"
        f"early = direct arrival to +{hc.EARLY_MS[1]:.0f} ms, late = +{hc.LATE_MS[0]:.0f} to "
        f"+{hc.LATE_MS[1]:.0f} ms. Numbers above the bars are the CHANGE against OFF in dB.\n"
        "Honest resolution: a band-pass smears time by about 1/bandwidth, printed under each band — "
        "80–160 Hz cannot separate early from late better than ~12 ms.\n"
        "315–630 Hz is the control: the rear chain barely reaches there, so what it shows is the "
        "method's own bias. A compensating front EQ scales early and late alike, so it cannot move this ratio.",
        fontsize=10)
    fig.tight_layout(rect=(0, 0.035, 1, 0.87))
    path = OUT / "h4-early-late-bars.png"
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    print(path)
    return held


# ---------------------------------------------------------------- figure 3
def level_change(mic, poses):
    """{tune: {'early': [...], 'late': [...]}} per third octave, dB vs OFF."""
    tunes = present(mic, poses)
    out = {fp: {"early": [], "late": []} for fp in tunes}
    for _centre, band in hc.THIRDS:
        per_pose = {fp: {"early": [], "late": []} for fp in tunes}
        for pose in poses:
            for fp in tunes:
                ir = impulse(mic, pose, fp, band)
                per_pose[fp]["early"].append(hc.window_energy(ir, *hc.EARLY_MS))
                per_pose[fp]["late"].append(hc.window_energy(ir, *hc.LATE_MS))
        for kind in ("early", "late"):
            for fp in tunes:
                changes = [10 * np.log10(max(c, 1e-30) / max(r, 1e-30))
                           for c, r in zip(per_pose[fp][kind], per_pose[hc.MUTED][kind])]
                out[fp][kind].append(float(np.mean(changes)))
    return out


def figure_three():
    centres = [c for c, _ in hc.THIRDS]
    table = level_change("main", FRONT)
    held = {hc.TUNES[fp][0]: {"third_octave_hz": centres, **table[fp]} for fp in table}
    fig, axes = plt.subplots(1, 2, figsize=(15.5, 6.0), sharey=True)
    for ax, kind, title in zip(axes, ("early", "late"), (
            "EARLY sound (direct arrival to +20 ms)\nwhat the listener hears first",
            "LATE sound (+20 to +200 ms)\nwhat the room sends back")):
        for fp in ORDER:
            if fp not in table or fp == hc.MUTED:
                continue
            label, colour, _style, width = hc.TUNES[fp]
            ax.plot(centres, table[fp][kind], marker="o", color=colour, lw=width, label=label)
        ax.axhline(0, color="#777777", ls="--", lw=1.2)
        ax.axvspan(100, 350, color="#ffe9a8", alpha=0.40, lw=0)
        ax.axvline(225, color="#333333", ls=":", lw=1.3)
        ax.set_xscale("log")
        ax.set_xticks(centres)
        ax.get_xaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter())
        ax.get_xaxis().set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax.grid(True, alpha=0.3)
        ax.set_title(title, fontsize=10)
        ax.set_xlabel("third octave, Hz", fontsize=9)
        ax.set_ylim(-13, 7)
    axes[0].set_ylabel("change against cardioid OFF, dB   (below 0 = quieter)", fontsize=9)
    axes[0].legend(fontsize=9, loc="lower right")
    fig.text(0.5, 0.015,
             "Dotted line: left of it a third octave rings LONGER than the 20 ms early/late split "
             "(63 Hz rings ~68 ms, 100 Hz ~43 ms, 250 Hz ~17 ms), so 'early' and 'late' are "
             "largely the same sound there. Read 250 Hz and up as a true split.",
             fontsize=8.5, color="#333333", ha="center")
    fig.suptitle(
        "FRONT microphone, mean of the 3 arm poses — does the room get less, for the same direct sound? "
        "Round 8ae2ac84b867 (H4)\n"
        "Each tune against cardioid OFF (rear woofer muted). Yellow = the 100–350 Hz target band. "
        "Levels are matched between tunes at 1–4 kHz, which no rear candidate can change.",
        fontsize=10)
    fig.tight_layout(rect=(0, 0.045, 1, 0.90))
    path = OUT / "h4-late-level.png"
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    print(path)
    return held


# ---------------------------------------------------------------- provenance
def provenance():
    held = {"markers_ms": {}, "alignment": {}, "noise": {}, "marker_sensitivity_db": {}}
    for (mic, pose), node in sorted(nodes.items()):
        key = f"{mic}|{hc.pose_angle(pose):+.0f}"
        muted = node["tunes"][hc.MUTED]
        held["markers_ms"][key] = {
            "used_1_4kHz_peak": node["marker"] / 48.0,
            "woofer_100_350_envelope_peak": float(np.argmax(np.abs(hc.hilbert(
                hc.band_limited_impulse(hc.GRID, muted, hc.WOOFER_BAND_HZ))))) / 48.0,
            "per_tune_own_marker_spread_ms": float(
                max(hc.marker_of(t) for t in node["tunes"].values())
                - min(hc.marker_of(t) for t in node["tunes"].values())) / 48.0}
        held["alignment"][key] = {hc.TUNES[fp][0]: {"delay_ms": f["delay_ms"],
                                                    "trim_db": f["trim_db"],
                                                    "residual_db": f["residual_db"],
                                                    "accepted": f["residual_db"] <= hc.il.ALIGN_RESIDUAL_MAX_DB}
                                  for fp, f in node["fits"].items()}
        held["noise"][key] = {}
        for name, band in hc.OCTAVES:
            ir = hc.rolled_impulse(muted, band, node["marker"])
            tail = hc.window_energy(ir, *TAIL_MS) / (TAIL_MS[1] - TAIL_MS[0])
            late = hc.window_energy(ir, *hc.LATE_MS) / (hc.LATE_MS[1] - hc.LATE_MS[0])
            pre = hc.window_energy(ir, -300.0, -100.0) / 200.0
            held["noise"][key][name] = {
                "late_over_decayed_tail_db": 10 * np.log10(max(late, 1e-30) / max(tail, 1e-30)),
                "late_over_pre_arrival_db": 10 * np.log10(max(late, 1e-30) / max(pre, 1e-30))}
    # does the headline number survive a 3 ms marker error?
    for mic, poses in (("main", FRONT), ("side", BEHIND)):
        for shift in (-3, 0, 3):
            for (m, p), node in nodes.items():
                if m == mic:
                    node["marker"] += int(shift * 48)
            table = early_late(mic, poses, hc.OCTAVES)
            held["marker_sensitivity_db"].setdefault(mic, {})[f"{shift:+d} ms"] = {
                hc.TUNES[fp][0]: {n: v[1] for n, v in table[fp].items()} for fp in table}
            for (m, p), node in nodes.items():
                if m == mic:
                    node["marker"] -= int(shift * 48)
    # and a 6/12 ms wider early window, which catches the band-pass pre-ringing
    held["early_window_sensitivity_db"] = {}
    for mic, poses in (("main", FRONT), ("side", BEHIND)):
        for start in (0.0, -6.0, -12.0):
            table = {}
            for name, band in hc.OCTAVES:
                ratios = {}
                for fp in present(mic, poses):
                    per_pose = []
                    for pose in poses:
                        ir = impulse(mic, pose, fp, band)
                        per_pose.append(10 * np.log10(
                            max(hc.window_energy(ir, start, hc.EARLY_MS[1]), 1e-30)
                            / max(hc.window_energy(ir, *hc.LATE_MS), 1e-30)))
                    ratios[fp] = float(np.mean(per_pose))
                for fp, value in ratios.items():
                    table.setdefault(hc.TUNES[fp][0], {})[name] = value - ratios[hc.MUTED]
            held["early_window_sensitivity_db"].setdefault(mic, {})[f"early starts {start:+.0f} ms"] = table
    return held


if __name__ == "__main__":
    from h4repeat import repeat_pairs
    repeats = repeat_pairs()
    worst = max(abs(v) for row in repeats.values() for v in row.values())
    payload = {
        "round": "round-8ae2ac84b867",
        "repeat_error_db": {"per_cell": repeats, "worst_abs_db": worst,
                            "note": ("attempt 2 minus attempt 1 of the four tunes captured twice, "
                                     "same muted reference and same marker: what the rig does to a "
                                     "number that should not have moved")},
        "note": ("A compensating front EQ for the OFF case scales early and late alike at each "
                 "frequency, so it cannot change an early/late RATIO. Rear-muted is therefore the "
                 "correct OFF reference for every ratio here."),
        "windows_ms": {"early": list(hc.EARLY_MS), "late": list(hc.LATE_MS),
                       "decayed_tail_used_as_floor": list(TAIL_MS)},
        "energy_time": figure_one(),
        "early_late_ratio": figure_two(worst),
        "front_level_change": figure_three(),
        "checks": provenance()}
    path = OUT / "h4-early-late.json"
    path.write_text(json.dumps(payload, indent=1, default=lambda v: v.item()) + "\n")
    print(path)
