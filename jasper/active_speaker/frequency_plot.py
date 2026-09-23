# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Render frequency-view documents."""

from __future__ import annotations

from pathlib import Path
from textwrap import fill
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.series_stats import power_mean_db
from jasper.json_fields import finite_float
from .frequency_display import merge_display_intervals, prepare_frequency_curve

DEFAULT_REF_BAND_HZ = (200.0, 5000.0)
LOW_BANDS_HZ = (20, 30, 40, 50, 60, 80, 120, 200, 500)


def prepare_plot_curve(curve: Mapping[str, Any], metadata: Mapping[str, Any] | None = None, *,
    ref_band_hz: Sequence[float] = DEFAULT_REF_BAND_HZ, normalize: bool = False,
) -> dict[str, Any]:
    if len(ref_band_hz) != 2 or not all(np.isfinite(ref_band_hz)) or not 0 < ref_band_hz[0] < ref_band_hz[1]:
        raise ValueError("reference band must have positive increasing edges")
    display = prepare_frequency_curve({**curve, "reference_db": 0}, metadata)["display"]
    freqs = np.asarray(curve["freqs_hz"], dtype=float)
    order = np.argsort(freqs, kind="stable")
    freqs = freqs[order]
    values = np.asarray(display["deviation_db"], dtype=float)[order]
    valid = np.isfinite(values)
    if curve.get("smoothing_fractional_octave") != 6:
        values[valid] = smooth_fractional_octave(freqs[valid], values[valid], fraction=6)
    ref_mask = valid & (freqs >= ref_band_hz[0]) & (freqs <= ref_band_hz[1])
    reference = None if normalize else finite_float(curve.get("reference_db"))
    mode = "run_reference" if reference is not None else "normalized"
    if reference is None and ref_mask.any():
        reference = power_mean_db(values[ref_mask])
    if reference is None:
        raise ValueError(f"{curve['id']}: no valid bins in the reference band")
    values -= reference
    measured = values[valid & (freqs >= 100) & (freqs <= 10000)]
    bands = []
    for lo, hi in zip(LOW_BANDS_HZ, LOW_BANDS_HZ[1:]):
        band = values[valid & (freqs >= lo) & (freqs < hi)]
        bands.append({"band_hz": [lo, hi], "mean_db": power_mean_db(band) if band.size else None})
    return {
        **display, "freqs_hz": freqs.tolist(), "display": mode,
        "deviation_db": [float(db) if ok else None for db, ok in zip(values, valid)],
        "reference_db": reference, "ref_band_hz": list(ref_band_hz), "smoothing_fractional_octave": 6,
        "rms_db": float(np.sqrt(np.mean(measured ** 2))) if measured.size else None,
        "peak_to_peak_db": float(np.ptp(measured)) if measured.size else None,
        "band_means": bands,
    }


def render_frequency_view(
    view: Mapping[str, Any], path: Path, *, selected: Sequence[str] = (),
    band_hz: Sequence[float] | None = None, ref_band_hz: Sequence[float] = DEFAULT_REF_BAND_HZ,
    low_end: bool = False, ylim_db: Sequence[float] = (-20, 20), normalize: bool = False,
) -> None:
    from matplotlib.figure import Figure  # lazy: optional plots extra
    from matplotlib.backends.backend_agg import FigureCanvasAgg  # lazy: optional plots extra
    from matplotlib.ticker import NullFormatter  # lazy: optional plots extra

    rows = [(run, curve) for run in view["runs"] for curve in run["series"]
            if not selected or f"{run['slot']}:{curve['id']}" in selected]
    if selected and set(selected) != {f"{r['slot']}:{c['id']}" for r, c in rows}:
        raise ValueError("unknown series selector; use slot:id from the frequency-view JSON")
    if not rows:
        raise ValueError("no selected frequency curves")
    if band_hz is not None and not 0 < band_hz[0] < band_hz[1]:
        raise ValueError("plot band must have positive increasing edges")
    poses: dict[tuple[str, str], list] = {}
    for run, curve in rows:
        position = curve.get("position") or {}
        pose = position.get("id") or str(position.get("deg", "unspecified"))
        plot = curve.get("plot", {})
        if normalize or plot.get("ref_band_hz") != list(ref_band_hz):
            plot = prepare_plot_curve(curve, run.get("metadata"), ref_band_hz=ref_band_hz, normalize=normalize)
        poses.setdefault((pose, curve.get("role", "")), []).append((run, curve, plot))
    impulses = [run for run in view["runs"] if run.get("metadata", {}).get("impulse")]
    nplots = len(poses) * (2 if low_end else 1)
    fig = Figure(figsize=(16 if low_end else 12, 3 * len(poses) + len(rows) * .4 + len(impulses) * 2.5), layout="constrained")
    FigureCanvasAgg(fig)
    grid = fig.add_gridspec(len(poses) + len(impulses) + 1, 2 if low_end else 1,
                           height_ratios=[3] * len(poses) + [2] * len(impulses) + [max(1, len(rows) * .4)])
    axes = [fig.add_subplot(grid[row, col]) for row in range(len(poses)) for col in range(2 if low_end else 1)]
    axes += [fig.add_subplot(grid[row, :]) for row in range(len(poses), grid.nrows)]
    labels = []
    colors: dict[str, str] = {}
    ticks = (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000)
    for pose_index, curves in enumerate(poses.values()):
        candidates = {str(curve["candidate_id"]) for _, curve, _ in curves if curve.get("candidate_id")}
        prefixes = {candidate: next((candidate[:size] for size in range(8, len(candidate))
                                    if sum(other.startswith(candidate[:size]) for other in candidates) == 1), candidate)
                    for candidate in candidates}
        position = curves[0][1].get("position") or {}
        offset, degrees = position.get("seat_offset_m"), position.get("deg")
        title = f"Seat {offset} m (right, forward, up)" if offset is not None else f"Pose {degrees}°" if degrees is not None else "Unspecified pose"
        if role := curves[0][1].get("role"):
            title += f" · {role}"
        if position.get("vertical_deg"):
            title += f" · elevation {position['vertical_deg']}°"
        panels = [(axes[pose_index * (2 if low_end else 1)], band_hz or (20, 20000))]
        if low_end:
            panels.append((axes[pose_index * 2 + 1], (20, 300)))
        for ax, limits in panels:
            untrusted = []
            legend: dict[str, Any] = {}
            for run, curve, display in curves:
                untrusted.extend(display["untrusted_intervals_hz"])
                candidate = str(curve.get("candidate_id") or "")
                base = curve.get("base", curve.get("configuration_kind") == "baseline" or curve.get("id") == "entry_baseline" or candidate == "base")
                label = "applied" if base else prefixes.get(candidate, curve["label"])
                identity = candidate or f"{run['slot']}:{curve['id']}"
                if run["measurement_family"] == "window_diagnostic":
                    label = curve["label"]
                    identity = f"{run['slot']}:{curve['id']}"
                color = colors.setdefault(identity, f"C{len(colors) % 10}")
                line, = ax.semilogx(display["freqs_hz"], display["deviation_db"], color=color,
                                   linestyle="--" if base else "-", label=label)
                legend.setdefault(identity, line)
            ax.axhline(0, color="#555", lw=.8)
            ax.set(xlabel="Frequency (Hz)", ylabel="Relative level (dB)", title=title, ylim=ylim_db)
            ax.set_xticks([hz for hz in ticks if limits[0] <= hz <= limits[1]],
                          [f"{hz // 1000}k" if hz >= 1000 else str(hz) for hz in ticks if limits[0] <= hz <= limits[1]])
            ax.xaxis.set_minor_formatter(NullFormatter())
            ax.set_xlim(*limits)
            ax.grid(True, which="both", alpha=.2)
            for lo, hi in merge_display_intervals(untrusted):
                if max(lo, limits[0]) < min(hi, limits[1]):
                    ax.axvspan(max(lo, limits[0]), min(hi, limits[1]), color="#888", alpha=.16)
                elif limits[0] <= lo == hi <= limits[1]:
                    ax.axvline(lo, color="#888", alpha=.16, lw=1)
            ax.legend(handles=list(legend.values()), fontsize=8, ncols=2)
    for index, (run, curve) in enumerate(rows):
        meta = run.get("metadata", {})
        candidate = curve.get("candidate_id") or "not recorded"
        if curve.get("identity_scope") == "prediction_from_basis":
            take = curve.get("basis_capture_id") or curve["id"]
            graph = curve.get("basis_graph_fingerprint") or "not recorded"
            identity = f"basis take: {take} | candidate: {candidate} | basis graph: {graph}"
        else:
            take = curve.get("take_id") or curve.get("capture_id") or curve["id"]
            graph = curve.get("graph_fingerprint") or meta.get("applied_graph_fingerprint") or "not recorded"
            identity = f"{take} | candidate: {candidate} | graph: {graph}"
        labels.append(fill(f"{index + 1}. {run['id']} | {identity}", 150))
    for ir_ax, run in zip(axes[nplots:-1], impulses):
        meta = run["metadata"]
        impulse = meta["impulse"]
        times = [(impulse["start_sample"] + i - impulse["time_reference_sample"]) * 1000 / meta["sample_rate_hz"] for i in range(len(impulse["samples"]))]
        ir_ax.plot(times, impulse["samples"], lw=.8)
        for curve in run["series"]:
            ir_ax.axvline(curve["window_ms"], alpha=.4)
        ir_ax.set(xlabel="Time from retained direct peak (ms)", ylabel="Impulse amplitude", title=f"{run['id']} · sample {meta['direct_peak_sample']}")
        ir_ax.grid(alpha=.2)
    axes[-1].axis("off")
    axes[-1].text(0, 1, "\n".join(labels) + "\n\nShaded areas are untrusted. Curves use 1/6-octave smoothing. Reference modes are recorded in the JSON.", va="top", fontsize=8, wrap=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140)
