# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Offline image rendering of the shared frequency-view document; no measurement DSP."""

from __future__ import annotations

from pathlib import Path
from textwrap import fill
from typing import Any, Mapping, Sequence


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
    labels = []
    for index, (run, curve) in enumerate(rows):
        meta = run.get("metadata", {})
        ref = curve.get("reference_db")
        if ref is None:
            raise ValueError(f"{curve['id']}: no display reference")
        band = curve.get("band_hz")
        floor = curve.get("validity_floor_hz") or meta.get("validity_floor_hz")
        low = max(band[0] if band else 0, floor or 0)
        high = band[1] if band else float("inf")
        excluded = curve.get("excluded_intervals_hz", meta.get("excluded_bands_hz", []))
        points = [(f, d - ref if low <= f <= high and not any(a <= f <= b for a, b in excluded) else float("nan"))
                  for f, d in zip(curve["freqs_hz"], curve["magnitude_db"])]
        if band_hz is not None:
            points = [(f, d) for f, d in points if band_hz[0] <= f <= band_hz[1]]
        if not points:
            raise ValueError(f"{curve['id']}: no saved bins in the plot band")
        color = f"C{index % 10}"
        ax.semilogx(*zip(*points), color=color, linestyle="--" if run["slot"] == "b" else "-", label=f"{index + 1}. {run['slot'].upper()} · {curve['label']}")
        smoothing = curve.get("smoothing_fractional_octave")
        window = curve.get("window_ms", curve.get("gate_window_ms"))
        take = curve.get("take_id") or curve.get("capture_id") or curve["id"]
        candidate = curve.get("candidate_id") or "not recorded"
        graph = curve.get("graph_fingerprint") or meta.get("applied_graph_fingerprint") or "not recorded"
        span = f"{low:g}–{high:g} Hz" if band or floor else "not recorded (only saved bins shown)"
        labels.append(fill(f"{index + 1}. {run['id']} | {take} | candidate: {candidate} | graph: {graph}", 150) + "\n"
                      f"    window: {str(window) + ' ms' if window is not None else 'not recorded'}; smoothing: {('1/' + str(smoothing) + ' octave') if smoothing else 'none' if smoothing == 0 else 'not recorded'}; valid: {span}; reference: {ref:.3f} dB")
    ax.axhline(0, color="#555", lw=.8, label="Flat display reference (0 dB)")
    ax.set(xlabel="Frequency (Hz)", ylabel="Level relative to saved reference (dB)", title="Frequency comparison · exact measurements remain the source of truth")
    ax.grid(True, which="both", alpha=.2)
    if band_hz is not None:
        ax.set_xlim(*band_hz)
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
    axes[-1].text(0, 1, "\n".join(labels) + "\n\nWindow coverage is not proof that reflections are absent. No additional smoothing or level fit is applied.", va="top", fontsize=8, wrap=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140)
