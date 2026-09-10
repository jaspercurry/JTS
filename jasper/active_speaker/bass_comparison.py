# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Compare explicitly selected bass takes on common qualified frequency bins."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from jasper.json_fields import finite_float

from .crossover_v2.measurement_context import CAPTURE_FIELDS, GRAPH_FIELDS, capture_basis, compare_capture_basis
from .crossover_v2.round_captures import doc_pose_key
from .measurement_bass import BASS_BANDS_HZ

CHANGE_FIELDS = {
    "candidate": GRAPH_FIELDS,
    "volume": ("level_db", "loudness_volume_db"),
    "demand": ("stimulus_dbfs", "stimulus_peak_dbfs", "stimulus_wav_sha256"),
    "diagnostic": (),
}


def _context(take: Mapping[str, Any]) -> dict[str, Any]:
    record = take["record"]
    return {
        **capture_basis(record), "pose_key": doc_pose_key(record),
        "position_axis": record.get("position_axis"),
        "mark_distance_m": record.get("mark_distance_m"),
        "loudness_volume_db": record.get("loudness_volume_db"),
        "sweep_band_hz": take["sweep_band_hz"], "sweep_duration_s": take["sweep_duration_s"],
        "analysis_calibration": take["calibration"],
    }


def _common(a: Mapping[str, Any], b: Mapping[str, Any], value: str, quality: str) -> tuple[np.ndarray, ...]:
    def arrays(item: Mapping[str, Any]) -> tuple[np.ndarray, ...]:
        f, y, q = (np.asarray(item[key], dtype=float) for key in ("freqs_hz", value, quality))
        if (f.ndim != 1 or len(f) < 2 or f.shape != y.shape or f.shape != q.shape
                or not np.isfinite(f).all() or f[0] <= 0 or not np.all(np.diff(f) > 0)):
            raise ValueError("bass_comparison_curve_invalid")
        return f, y, (q == 1) & np.isfinite(y)
    af, ay, aq = arrays(a)
    bf, by, bq = arrays(b)
    # Interpolate the full mask, including holes: both bracketing bins must qualify.
    mask = aq & (np.interp(np.log(af), np.log(bf), bq.astype(float), left=0, right=0) >= 1 - 1e-12)
    f = af[mask]
    return f, ay[mask], np.interp(np.log(f), np.log(bf), by)


def compare_bass_takes(before: Mapping[str, Any], after: Mapping[str, Any], *, change: str) -> dict[str, Any]:
    interventions = CHANGE_FIELDS[change]
    required = tuple(dict.fromkeys((*CAPTURE_FIELDS, *GRAPH_FIELDS, "loudness_volume_db",
        "pose_key", "position_axis", "mark_distance_m", "speaker_candidate_id", "sweep_band_hz", "sweep_duration_s", "analysis_calibration")))
    context = compare_capture_basis(_context(after), _context(before), interventions=interventions,
                                    required=tuple(key for key in required if key not in interventions))
    result: dict[str, Any] = {"schema": "jts_bass_comparison/1", "change": change, "context": context,
        "before": before["record_path"], "after": after["record_path"], "bands": []}
    if context["incompatible_fields"] and change != "diagnostic":
        result.update(available=False, reason="capture_context_changed")
        return result
    f, a, b = _common(before, after, "fundamental_db", "fundamental_qualified")
    records = (before["record"], after["record"])
    stimulus = [finite_float(record.get("stimulus_dbfs")) for record in records]
    volume = [finite_float(record.get("level_db")) for record in records]
    stimulus_delta = stimulus[1] - stimulus[0] if stimulus[0] is not None and stimulus[1] is not None else None
    input_delta = stimulus_delta + volume[1] - volume[0] if stimulus_delta is not None and volume[0] is not None and volume[1] is not None else None
    delta = b - a
    harmonics = {order: _common(before["harmonics"][order], after["harmonics"][order], "relative_db", "qualified")
                 for order in before["harmonics"].keys() & after["harmonics"].keys()}
    for lo, hi in BASS_BANDS_HZ:
        mask = (f >= lo) & (f < hi)
        transfer = float(np.median(delta[mask])) if mask.any() else None
        output = transfer + stimulus_delta if transfer is not None and stimulus_delta is not None else None
        row: dict[str, Any] = {"band_hz": [lo, hi], "qualified_bins": int(mask.sum()),
            "transfer_change_db": transfer, "fundamental_output_change_db": output,
            "combined_compression_db": input_delta - output if input_delta is not None and output is not None and change in {"volume", "demand"} else None,
            "harmonics": {}}
        for order, (hf, ha, hb) in harmonics.items():
            hm = (hf >= lo) & (hf < hi)
            row["harmonics"][order] = {"qualified_bins": int(hm.sum()),
                "before_relative_db": float(np.median(ha[hm])) if hm.any() else None,
                "after_relative_db": float(np.median(hb[hm])) if hm.any() else None,
                "change_db": float(np.median((hb - ha)[hm])) if hm.any() else None}
        result["bands"].append(row)
    result.update(available=bool(f.size), freqs_hz=f.tolist(), transfer_change_db=delta.tolist(),
                  requested_input_change_db=input_delta,
                  interpretation="Combined compression includes intended DSP action; it is not an isolated driver limit. Diagnostic differences do not isolate room transfer.")
    return result


def selected_take(view: Mapping[str, Any], take_id: str) -> Mapping[str, Any]:
    if view.get("schema") != "jts_bass_view/1":
        raise ValueError("bass_view_schema_invalid")
    matches = [take for take in view["takes"] if take["record"].get("take_id") == take_id]
    if len(matches) != 1:
        raise ValueError("bass_comparison_exact_take_required")
    return matches[0]
