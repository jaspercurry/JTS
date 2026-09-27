"""Scratch probe (investigation only, never committed): the proposed ONE headroom
accounting, evaluated on an EMITTED graph, next to today's additive charge."""
from __future__ import annotations

import copy
import math
from typing import Any, Mapping

import numpy as np
import yaml

from jasper.active_speaker.branch_chain import CHAIN_GRID_HZ, camilla_evaluation_grid, headroom_charge_db
from jasper.active_speaker.graph_transfer import complex_channel_transfer

HEADROOM = "active_baseline_headroom"


def _strip_preference(graph: dict[str, Any]) -> dict[str, Any]:
    """Drop the pre-split steps between the headroom gain and the split mixer
    (the preference EQ, ADR-0121), and zero the headroom gain itself."""
    g = copy.deepcopy(graph)
    if HEADROOM in (g.get("filters") or {}):
        g["filters"][HEADROOM]["parameters"]["gain"] = 0.0
    out, after_headroom = [], False
    for step in g["pipeline"]:
        if step.get("type") == "Mixer" and str(step.get("name", "")).startswith("split_active_"):
            after_headroom = False
        if step.get("type") == "Filter" and HEADROOM in (step.get("names") or []):
            after_headroom = True
            out.append(step)
            continue
        if after_headroom and step.get("type") == "Filter":
            continue
        out.append(step)
    g["pipeline"] = out
    return g


def program_peak(graph: Mapping[str, Any], *, keep_headroom: bool = False) -> tuple[float, int | None, float]:
    """Max over outputs of sum-over-inputs |H(f)|, dB, with (output, Hz)."""
    g = _strip_preference(dict(graph))
    if keep_headroom and HEADROOM in (graph.get("filters") or {}):
        g["filters"][HEADROOM]["parameters"]["gain"] = graph["filters"][HEADROOM]["parameters"]["gain"]
    used = {n for s in g["pipeline"] if s.get("type") == "Filter" for n in (s.get("names") or [])}
    specs = [g["filters"][n] for n in used
             if g["filters"].get(n, {}).get("type") in ("Biquad", "BiquadCombo")
             and not n.startswith("bass_ext_dynamic")]
    grid = np.unique(np.concatenate([CHAIN_GRID_HZ, camilla_evaluation_grid(specs)]))
    grid = grid[(grid > 0.0) & (grid < 24000.0)]
    cap = int(g["devices"]["capture"]["channels"])
    width = int(g["devices"]["playback"]["channels"])
    total = {o: np.zeros(grid.shape) for o in range(width)}
    for c in range(cap):
        resp = complex_channel_transfer(
            g, grid, input_weights={c: 1.0}, output_channels={o: o for o in range(width)},
            allow_limiter_passthrough=True, dynamic_bass_at_rest=True,
        )
        for o in range(width):
            total[o] = total[o] + np.abs(resp[o])
    best = (-math.inf, None, math.nan)
    for o, mag in total.items():
        if not np.any(mag > 0):
            continue
        db = 20.0 * np.log10(np.maximum(mag, 1e-15))
        i = int(np.argmax(db))
        if db[i] > best[0]:
            best = (float(db[i]), o, float(grid[i]))
    return best


def charges(yaml_text: str, *, output_trim_db: float = 0.0, baseline_headroom_db: float = 0.0) -> dict[str, Any]:
    graph = yaml.safe_load(yaml_text)
    old = -float(graph["filters"][HEADROOM]["parameters"]["gain"]) if HEADROOM in graph["filters"] else 0.0
    peak, out, hz = program_peak(graph)
    new = baseline_headroom_db + headroom_charge_db(peak) + max(0.0, output_trim_db)
    charged_peak, _, _ = program_peak(graph, keep_headroom=True)
    return {"old_db": round(old, 4), "new_db": round(new, 4), "delta_db": round(new - old, 4),
            "peak_db": round(peak, 4), "peak_output": out, "peak_hz": round(hz, 1),
            "old_graph_charged_peak_db": round(charged_peak, 4)}
