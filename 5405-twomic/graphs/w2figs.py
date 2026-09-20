#!/usr/bin/env python3
"""The three wall early/late figures, plus the JSON behind them."""
from __future__ import annotations

import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt          # noqa: E402
import matplotlib.ticker                 # noqa: E402
import numpy as np                       # noqa: E402

import w2core as wc                      # noqa: E402
from w2load import rows                  # noqa: E402

OUT = wc.SP / "graphs"
DPI = 110
SHOW_MS = 150.0
ROUND = "wall2"

held = rows()
nodes = wc.cells(held)


def node_of(mic, tag=ROUND):
    return nodes[(tag, mic)]


def plain_hz(ax, ticks):
    """The owner's style: plain Hz numbers, never exponent ticks."""
    ax.set_xscale("log")
    ax.set_xticks(ticks)
    ax.get_xaxis().set_major_formatter(matplotlib.ticker.FuncFormatter(
        lambda v, _p: f"{v / 1000:g}k" if v >= 1000 else f"{v:g}"))
    ax.get_xaxis().set_minor_formatter(matplotlib.ticker.NullFormatter())


# ---------------------------------------------------------------- figure 1
def energy_time(mic):
    span = int(SHOW_MS * 1e-3 * wc.FS)
    node = node_of(mic)
    reference = np.mean([wc.envelope_db(wc.rolled_impulse(t, wc.BAND_100_350, node["marker"]))
                         for t in node["tunes"][wc.REFERENCE]], axis=0)
    top = float(np.max(reference))
    envelopes, cumulative = {}, {}
    for tune in wc.ORDER:
        takes = node["tunes"][tune]
        envelopes[tune] = 10 * np.log10(np.maximum(np.mean(
            [wc.envelope_db(wc.rolled_impulse(t, wc.BAND_100_350, node["marker"]))
             for t in takes], axis=0)[:span] / top, 1e-12))
        shares = []
        for transfer in takes:
            ir = wc.rolled_impulse(transfer, wc.BAND_100_350, node["marker"])
            energy = ir[:int(wc.LATE_END_MS * 1e-3 * wc.FS)] ** 2
            shares.append(np.cumsum(energy)[:span] / max(float(np.sum(energy)), 1e-30))
        cumulative[tune] = 100.0 * np.mean(shares, axis=0)
    return np.arange(span) / wc.FS * 1e3, envelopes, cumulative


def figure_one():
    fig, axes = plt.subplots(2, 2, figsize=(15.5, 9), sharex=True)
    out = {}
    for col, (mic, where) in enumerate(wc.MICS):
        time, envelopes, cumulative = energy_time(mic)
        out[mic] = {"time_ms": time[::48].tolist(),
                    "envelope_db_re_B0_peak": {wc.label(k): v[::48].tolist()
                                               for k, v in envelopes.items()},
                    "cumulative_pct": {wc.label(k): v[::48].tolist()
                                       for k, v in cumulative.items()}}
        for row, (data, ylabel, limits) in enumerate((
                (envelopes, "energy, dB   (0 dB = B0's own direct peak at this mic)", (-40, 4)),
                (cumulative, "% of the energy in the first 250 ms", (0, 102)))):
            ax = axes[row][col]
            for tune in wc.ORDER:
                name, colour, style, width = wc.TUNES[tune]
                ax.plot(time, data[tune], color=colour, ls=style, lw=width, label=name)
            for mark in (20.0, 40.0):
                ax.axvline(mark, color="#444444", ls=":", lw=1.1)
                ax.text(mark, limits[1], f" {mark:.0f} ms", fontsize=8, va="top", color="#444444")
            ax.set_xlim(0, SHOW_MS)
            ax.set_ylim(*limits)
            ax.grid(True, alpha=0.3)
            ax.set_ylabel(ylabel, fontsize=9)
            if row == 0:
                ax.set_title(where, fontsize=10)
            else:
                ax.set_xlabel("ms after the direct arrival", fontsize=9)
    axes[0][0].legend(fontsize=9, loc="upper right")
    fig.suptitle(
        "How the 100–350 Hz sound arrives at the wall — round wall2 (dfe333aea6d3), speaker 0.2 m from the wall\n"
        "0 dB is B0's own direct peak at that microphone, so each panel is read against its own rear-off reference; "
        "the two mics are not comparable with each other.\n"
        "Hilbert envelope of the zero-phase 100–350 Hz impulse, smoothed 3 ms, both repeat takes averaged. "
        "t=0 is the direct arrival (B0's 1–4 kHz envelope peak, shared by every tune).\n"
        "A 100–350 Hz band-pass rings about 4 ms, so nothing here resolves time finer than that.",
        fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.89))
    path = OUT / "wall2-energy-time.png"
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    print(path)
    return out


# ---------------------------------------------------------------- figure 2
def ratio_table(mic, split, tag=ROUND):
    node = nodes[(tag, mic)]
    out = {}
    for tune in node["tunes"]:
        reference = {n: wc.ratio_db(node, wc.REFERENCE, b, split) for n, b in wc.OCTAVES}
        out[tune] = {n: (wc.ratio_db(node, tune, b, split),
                         wc.ratio_db(node, tune, b, split) - reference[n])
                     for n, b in wc.OCTAVES}
    return out


def figure_two(repeat_db):
    fig, axes = plt.subplots(1, 2, figsize=(16, 7.6), sharey=True)
    names = [n for n, _ in wc.OCTAVES]
    tables = {mic: {s: ratio_table(mic, s) for s in wc.SPLITS} for mic, _ in wc.MICS}
    values = [v[0] for mic in tables for t in wc.ORDER for v in tables[mic][20.0][t].values()]
    low, top = min(values), max(values)
    limits = (low - 1.2, top + 6.0)
    out = {}
    for ax, (mic, where) in zip(axes, wc.MICS):
        out[mic] = {f"{s:.0f} ms split": {
            wc.label(t): {n: {"early_late_db": v[0], "change_vs_B0_db": v[1]}
                          for n, v in tables[mic][s][t].items()}
            for t in tables[mic][s]} for s in wc.SPLITS}
        width = 0.8 / len(wc.ORDER)
        base = np.arange(len(names))
        for index, tune in enumerate(wc.ORDER):
            name, colour, _style, _lw = wc.TUNES[tune]
            offset = base + (index - (len(wc.ORDER) - 1) / 2) * width
            ax.bar(offset, [tables[mic][20.0][tune][n][0] for n in names], width * 0.9,
                   color=colour, label=name, edgecolor="white", linewidth=0.6)
            for x, band in zip(offset, names):
                if tune == wc.REFERENCE:
                    text = "B0 ref"
                else:
                    text = (f"{tables[mic][20.0][tune][band][1]:+.1f}\n"
                            f"({tables[mic][30.0][tune][band][1]:+.1f})")
                ax.text(x, top + 0.7, text, rotation=90, fontsize=7.4, ha="center",
                        va="bottom", color="#333333" if tune == wc.REFERENCE else colour)
        ax.set_xticks(base)
        ax.set_xticklabels([f"{n}\n(rings ~{wc.RING_MS[n]:.0f} ms)"
                            + ("\nCONTROL" if n == "315-630 Hz" else "") for n in names],
                           fontsize=9)
        ax.axhline(0, color="#777777", lw=1.0)
        ax.grid(True, axis="y", alpha=0.3)
        ax.set_title(where, fontsize=10)
        ax.set_ylim(*limits)
    axes[0].set_ylabel("early-to-late energy ratio, dB   (higher = more direct, less room)", fontsize=9)
    axes[0].legend(fontsize=9, loc="lower left")
    fig.text(0.5, 0.035,
             "Numbers above the bars: change against B0 in dB with the 20 ms split, and in brackets "
             "with a 30 ms split. Noise yardstick: the same tunes measured 2 h earlier (wall1c)\n"
             f"move this number by {repeat_db:.2f} dB at worst. B0 and C0 differ ONLY in front EQ, "
             "so the grey bar's distance from the blue one is how far a front EQ alone can shift "
             "this number — treat it as the floor for reading any other bar.",
             fontsize=8.5, color="#333333", ha="center")
    fig.suptitle(
        "Early versus late energy at the wall — round wall2, B0 (rear off, front matched) against each tune\n"
        f"early = direct arrival to +20 ms, late = +20 to +{wc.LATE_END_MS:.0f} ms. Both repeat takes "
        "power-averaged; a ratio is taken inside one take, so the level trim divides out of it.\n"
        "Honest resolution: a band-pass smears time by about 1/bandwidth, printed under each band — "
        "80–160 Hz cannot separate early from late better than ~12 ms.\n"
        "315–630 Hz is the control: the rear branch is low-passed near 300 Hz, so what moves there is the method's own bias.",
        fontsize=10)
    fig.tight_layout(rect=(0, 0.065, 1, 0.87))
    path = OUT / "wall2-early-late-bars.png"
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    print(path)
    return out


# ---------------------------------------------------------------- figure 3
def figure_three():
    centres = [c for c, _ in wc.THIRDS]
    node = node_of("side")
    #: C0 rides along as the CONTROL: it differs from B0 by front EQ alone, so a
    #: front EQ's own footprint on these two panels is whatever the grey line does.
    shown = ("62a97fbc", "1b2915e5", "1feb7466")
    table = {t: {kind: [wc.level_change_db(node, t, band, 20.0, kind) for _c, band in wc.THIRDS]
                 for kind in ("early", "late")} for t in shown}
    fig, axes = plt.subplots(1, 2, figsize=(15.5, 6.2), sharey=True)
    for ax, kind, title in zip(axes, ("early", "late"), (
            "EARLY sound (direct arrival to +20 ms)\nwhat reaches the seat first",
            f"LATE sound (+20 to +{wc.LATE_END_MS:.0f} ms)\nwhat the room sends back")):
        for tune in shown:
            name, colour, style, width = wc.TUNES[tune]
            suffix = " — CONTROL, front EQ only" if tune == "62a97fbc" else ""
            ax.plot(centres, table[tune][kind], marker="o", ms=4, color=colour, ls=style,
                    lw=width, label=name + suffix)
        ax.axhline(0, color="#5b7fa6", ls="--", lw=1.6)
        ax.axvspan(100, 350, color="#ffe9a8", alpha=0.40, lw=0)
        ax.axvline(225, color="#333333", ls=":", lw=1.3)
        plain_hz(ax, centres)
        ax.grid(True, alpha=0.3)
        ax.set_title(title, fontsize=10)
        ax.set_xlabel("third octave, Hz", fontsize=9)
        ax.set_ylim(-11, 9)
    axes[0].set_ylabel("change against B0, dB   (0 dB = B0, the fair rear-off)", fontsize=9)
    axes[0].legend(fontsize=8.5, loc="lower right")
    fig.text(0.5, 0.035,
             "Dotted line: left of it a third octave rings LONGER than the 20 ms split (63 Hz ~68 ms, "
             "100 Hz ~43 ms, 250 Hz ~17 ms), so 'early' and 'late' are largely the same sound there.\n"
             "Each change is paired within a measurement pass, which removes the ~1.7 dB level drift "
             "between this round's two passes. The grey control moves both panels together, which is "
             "what a front EQ alone does.",
             fontsize=8.5, color="#333333", ha="center")
    fig.suptitle(
        "AT THE LISTENING SEAT — does the room get less, for the sound that arrives first? Round wall2\n"
        "0 dB is B0, the FAIR rear-off (rear muted, front chain matched to the cardioid tunes). "
        "Yellow = the 100–350 Hz band the rear branch works in.",
        fontsize=10)
    fig.tight_layout(rect=(0, 0.07, 1, 0.90))
    path = OUT / "wall2-late-level.png"
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    print(path)
    return {wc.label(t): {"third_octave_hz": centres, **table[t]} for t in table}


# ---------------------------------------------------------------- checks
def between_round_repeat():
    """wall1c against wall2 for the tunes both rounds hold: the noise yardstick."""
    shared = sorted(set(nodes[("wall1c", "side")]["tunes"]) & set(nodes[("wall2", "side")]["tunes"]))
    out, worst = {}, 0.0
    for mic, _where in wc.MICS:
        for tune in shared:
            row = {}
            for name, band in wc.OCTAVES:
                gap = (wc.ratio_db(nodes[("wall2", mic)], tune, band, 20.0)
                       - wc.ratio_db(nodes[("wall1c", mic)], tune, band, 20.0))
                row[name] = gap
                worst = max(worst, abs(gap))
            out[f"{mic}|{wc.label(tune)}"] = row
    return out, worst


def b0_c0_sanity():
    """B0 and C0 differ only in front EQ, which cannot move a ratio. Free test."""
    out = {}
    for mic, _where in wc.MICS:
        for split in wc.SPLITS:
            table = ratio_table(mic, split)
            out[f"{mic}|{split:.0f} ms split"] = {
                n: table["62a97fbc"][n][1] for n, _b in wc.OCTAVES}
    return out


def provenance():
    out = {"markers_ms": {}, "alignment": {}, "noise": {}}
    for (tag, mic), node in sorted(nodes.items()):
        key = f"{tag}|{mic}"
        own = [f["own_marker_ms"] for fits in node["fits"].values() for f in fits]
        out["markers_ms"][key] = {"shared_marker_ms": node["marker"] / 48.0,
                                  "own_marker_min_ms": min(own), "own_marker_max_ms": max(own)}
        out["alignment"][key] = {wc.label(t): node["fits"][t] for t in node["fits"]}
        out["noise"][key] = {}
        for name, band in wc.OCTAVES:
            ir = wc.rolled_impulse(node["tunes"][wc.REFERENCE][0], band, node["marker"])
            late = wc.window_energy(ir, 20.0, wc.LATE_END_MS) / (wc.LATE_END_MS - 20.0)
            tail = wc.window_energy(ir, *wc.TAIL_MS) / (wc.TAIL_MS[1] - wc.TAIL_MS[0])
            out["noise"][key][name] = 10 * np.log10(max(late, 1e-30) / max(tail, 1e-30))
    return out


if __name__ == "__main__":
    repeats, worst = between_round_repeat()
    payload = {
        "rounds": {"wall2": "round-dfe333aea6d3", "wall1c": "round-0d0abbb03574"},
        "rig": ("speaker 0.2 m from a wall; main = UMIK-2 front arm 0.81 m pose 0; "
                "side = Dayton fixed at the listening seat ~2 m, ~20 deg off axis, in front"),
        "reference": "B0 1f65d837 (rear off, front chain matched to the cardioid tunes)",
        "note": ("A front EQ scales early and late alike at each frequency, so it cannot move an "
                 "early/late RATIO. B0 and C0 differ only in front chain, so their ratios must "
                 "agree -- that is the free sanity test in b0_vs_c0_ratio_change_db."),
        "windows_ms": {"early_from_arrival_to": list(wc.SPLITS),
                       "late_end": wc.LATE_END_MS, "decayed_tail_floor": list(wc.TAIL_MS)},
        "energy_time": figure_one(),
        "early_late_ratio": figure_two(worst),
        "seat_level_change": figure_three(),
        "b0_vs_c0_ratio_change_db": b0_c0_sanity(),
        "between_round_repeat_db": {"per_tune": repeats, "worst_abs_db": worst,
                                    "note": "wall2 minus wall1c, same tune, 20 ms split, ~2 h apart"},
        "checks": provenance()}
    path = OUT / "wall2-early-late.json"
    path.write_text(json.dumps(payload, indent=1, default=lambda v: v.item()) + "\n")
    print(path)
