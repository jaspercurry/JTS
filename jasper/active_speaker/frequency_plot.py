# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Offline image rendering of the shared frequency-view document; no measurement DSP."""

from __future__ import annotations

from pathlib import Path
from textwrap import fill
from typing import Any, Mapping, Sequence

from .frequency_display import merge_display_intervals, prepare_frequency_curve


def render_frequency_view(view: Mapping[str, Any], path: Path, *, selected: Sequence[str] = (), band_hz: Sequence[float] | None = None) -> None:
    from matplotlib.figure import Figure  # lazy: optional laptop plotting dependency
    from matplotlib.backends.backend_agg import FigureCanvasAgg  # lazy: optional plotting dependency

    rows = [(run, curve) for run in view["runs"] for curve in run["series"]
            if not selected or f"{run['slot']}:{curve['id']}" in selected]
    if selected and set(selected) != {f"{r['slot']}:{c['id']}" for r, c in rows}:
        raise ValueError("unknown series selector; use slot:id from the frequency-view JSON")
    if not rows:
        raise ValueError("no selected frequency curves")
    if band_hz is not None and not 0 < band_hz[0] < band_hz[1]:
        raise ValueError("plot band must have positive increasing edges")
    impulses = [run for run in view["runs"] if run.get("metadata", {}).get("impulse")]
    fig = Figure(figsize=(12, 6 + len(rows) * .55 + len(impulses) * 2.5), layout="constrained")
    FigureCanvasAgg(fig)
    axes = fig.subplots(2 + len(impulses), 1, gridspec_kw={"height_ratios": [4, *([2] * len(impulses)), max(1.5, len(rows) * .55)]})
    ax = axes[0]
    labels, untrusted = [], []
    for index, (run, curve) in enumerate(rows):
        meta = run.get("metadata", {})
        display = prepare_frequency_curve(curve, meta)["display"]
        ref = curve.get("reference_db")
        if ref is None:
            raise ValueError(f"{curve['id']}: no display reference")
        low, high = display["valid_band_hz"]
        untrusted.extend(display["untrusted_intervals_hz"])
        points = list(zip(curve["freqs_hz"], display["deviation_db"]))
        if band_hz is not None:
            points = [(f, d) for f, d in points if band_hz[0] <= f <= band_hz[1]]
        if not any(d is not None for _, d in points):
            raise ValueError(f"{curve['id']}: no saved bins in the plot band")
        color = f"C{index % 10}"
        ax.semilogx(*zip(*points), color=color, linestyle="--" if run["slot"] == "b" else "-", label=f"{index + 1}. {run['slot'].upper()} · {curve['label']}")
        smoothing = curve.get("smoothing_fractional_octave")
        window = curve.get("window_ms", curve.get("gate_window_ms"))
        candidate = curve.get("candidate_id") or "not recorded"
        if curve.get("identity_scope") == "prediction_from_basis":
            take = curve.get("basis_capture_id") or curve["id"]
            graph = curve.get("basis_graph_fingerprint") or "not recorded"
            identity = f"basis take: {take} | candidate: {candidate} | basis graph: {graph}"
            if curve.get("measured_capture_id"):
                measured_graph = curve.get("measured_graph_fingerprint") or "not recorded"
                identity += (
                    f" | measured take: {curve['measured_capture_id']}"
                    f" | measured graph: {measured_graph}"
                )
        else:
            take = curve.get("take_id") or curve.get("capture_id") or curve["id"]
            graph = curve.get("graph_fingerprint") or meta.get("applied_graph_fingerprint") or "not recorded"
            identity = f"{take} | candidate: {candidate} | graph: {graph}"
        span = f"{low:g}–{high if high is not None else 'unspecified'} Hz" if low or high else "not recorded (only saved bins shown)"
        labels.append(fill(f"{index + 1}. {run['id']} | {identity}", 150) + "\n"
                      f"    window: {str(window) + ' ms' if window is not None else 'not recorded'}; smoothing: {('1/' + str(smoothing) + ' octave') if smoothing else 'none' if smoothing == 0 else 'not recorded'}; valid: {span}; reference: {ref:.3f} dB")
    ax.axhline(0, color="#555", lw=.8, label="Flat display reference (0 dB)")
    ax.set(xlabel="Frequency (Hz)", ylabel="Level relative to saved reference (dB)", title="Frequency comparison · exact measurements remain the source of truth")
    ax.grid(True, which="both", alpha=.2)
    limits = band_hz or ax.get_xlim()
    for lo, hi in merge_display_intervals(untrusted):
        if max(lo, limits[0]) < min(hi, limits[1]):
            ax.axvspan(max(lo, limits[0]), min(hi, limits[1]), color="#888", alpha=.16)
        elif limits[0] <= lo == hi <= limits[1]:
            ax.axvline(lo, color="#888", alpha=.16, lw=1)
    ax.set_xlim(*limits)
    ax.legend(fontsize=8, ncols=2)
    for ir_ax, run in zip(axes[1:-1], impulses):
        meta = run["metadata"]
        impulse = meta["impulse"]
        times = [(impulse["start_sample"] + i - impulse["time_reference_sample"]) * 1000 / meta["sample_rate_hz"] for i in range(len(impulse["samples"]))]
        ir_ax.plot(times, impulse["samples"], lw=.8)
        for curve in run["series"]:
            ir_ax.axvline(curve["window_ms"], alpha=.4)
        ir_ax.set(xlabel="Time from retained direct peak (ms)", ylabel="Impulse amplitude", title=f"{run['id']} · sample {meta['direct_peak_sample']}")
        ir_ax.grid(alpha=.2)
    axes[-1].axis("off")
    axes[-1].text(0, 1, "\n".join(labels) + "\n\nShaded areas are untrusted. Window coverage does not prove reflections are absent. No additional smoothing or level fit is applied.", va="top", fontsize=8, wrap=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140)
