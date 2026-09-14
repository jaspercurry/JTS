# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Gate disclosures, summary numbers and the saved round's readable index."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from jasper.audio_measurement.gating import f_trusted_floor_hz
from jasper.audio_measurement.spatial_combine import octave_bands_hz
from jasper.json_fields import finite_float

from .crossover_v2.round_inputs import SetTakes
from .flat_spec import _power_mean_db
from .measurement_programs import POSE_KIND_BEARING, PURPOSE_SPEAKER, run_purpose

PACKET_FILENAME = "packet.json"
PICTURE_FILENAME = "frequency.png"
INDEX_FILENAME = "index.md"


def gate_fields(take: Mapping[str, Any]) -> dict[str, Any]:
    curve = take.get("curve") or {}
    window = finite_float(curve.get("gate_window_ms"))
    return {**{key: curve.get(key) for key in ("gate_window_ms", "validity_floor_hz", "floor_source")},
            "trusted_floor_hz": finite_float(f_trusted_floor_hz(window / 1000)) if window is not None else None}


def series_stats(plot: Mapping[str, Any], trusted_floor_hz: float | None) -> dict[str, Any]:
    def number(value: float | None, lo_hz: float) -> dict[str, Any]:
        return {"value": value, "below_trusted_floor": value is not None
                and trusted_floor_hz is not None and lo_hz < trusted_floor_hz}

    freqs = np.asarray(plot["freqs_hz"], dtype=float)
    values = np.asarray(plot["deviation_db"], dtype=float)
    valid = np.isfinite(values) & (freqs > 0)
    measured = valid & (freqs >= 100) & (freqs <= 10000)
    bands = {}
    for center, lo, hi in octave_bands_hz(20, 20000):
        band = values[valid & (freqs >= lo) & (freqs < hi)]
        bands[f"{center:g}"] = number(_power_mean_db(band) if band.size else None, lo)
    return {
        "rms_100_10k_db": number(plot["rms_db"], 100),
        "tilt_db_per_decade": number(float(np.polyfit(np.log10(freqs[measured]), values[measured], 1)[0])
                                     if np.unique(freqs[measured]).size >= 2 else None, 100),
        "band_means_db": bands,
        "low_end_means_db": {f"{b['band_hz'][0]}_{b['band_hz'][1]}": number(b["mean_db"], b["band_hz"][0])
                             for b in plot["band_means"]},
    }


def _decision(contract: Mapping[str, Any]) -> str:
    schema = contract.get("schema", {})
    fields: dict[str, Any] = {}

    def visit(node: Mapping[str, Any], path: str) -> None:
        constraints = {key: node[key] for key in ("enum", "const", "minimum", "maximum", "maxItems") if key in node}
        if constraints:
            fields[path] = constraints
        for name, child in node.get("properties", {}).items():
            visit(child, f"{path}.{name}" if path else name)
        for key, suffix in (("items", "[]"), ("additionalProperties", ".*")):
            if isinstance(node.get(key), Mapping):
                visit(node[key], path + suffix)

    visit(schema, "")
    bounds = {key: value if len(json.dumps(value)) < 350 else "see packet.json limits"
              for key, value in contract.get("bounds", {}).items()}
    return json.dumps({"required": schema.get("required", []), "fields": fields, "bounds": bounds}, separators=(",", ":"))



def _span(rows: list[Mapping[str, Any]], key: str) -> str:
    """One value when the takes agree, else the range they cover."""
    values = [row.get(key) for row in rows]
    distinct = sorted({str(value) for value in values})
    if len(distinct) == 1:
        return distinct[0]
    numbers = [value for value in values if isinstance(value, (int, float))]
    if len(numbers) == len(values):
        return f"{min(numbers)}–{max(numbers)}"
    return ", ".join(distinct)

def packet_index(
    packet: Mapping[str, Any], target: Path, views: list[dict[str, Any]], manifest: Mapping[str, Any],
) -> str:
    commands = [shlex.join(["jasper-round-views", row["view"], str(target), *(["--set", row["set_id"]] if row.get("set_id") else []),
                            *(["--incumbent", row["incumbent_set_id"]] if row.get("incumbent_set_id") else [])])
                for row in views if row["view"] != "frequency"]
    if packet["artifacts"]["frequency_view"]:
        commands.append(shlex.join(["jasper-round-views", "frequency", packet["artifacts"]["frequency_view"],
                                   "--image", str(target / PICTURE_FILENAME)]))
    commands += [shlex.join(["jasper-round-views", "speaker-fit", str(target), "--set", fit["set_id"], "--take", fit["take_id"]])
                 for fit in packet["fits"]]
    for group in manifest.get("sets", ()):
        bearings = {(take["pose"].get("deg"), take["pose"].get("elevation_deg"), take["pose"].get("distance_m"))
                    for take in SetTakes.from_row(group).takes
                    if take["selected"] and take["pose"].get("kind") == POSE_KIND_BEARING}
        if len(bearings) >= 2:
            commands.append(shlex.join(["jasper-round-views", "sweep", str(target), "--scope", "round", "--set", group["set_id"]]))
    decisions: dict[str, dict[str, list[str]]] = {}
    for set_id, limits in packet["limits"].items():
        if limits.get("status") == "unavailable":
            decisions.setdefault("unavailable", {}).setdefault(limits["reason"], []).append(set_id)
        sections = limits if packet["program"] and run_purpose(packet["program"]) == PURPOSE_SPEAKER else {"decision": limits}
        for name, contract in sections.items():
            if isinstance(contract, Mapping) and "schema" in contract:
                decisions.setdefault(name, {}).setdefault(_decision(contract), []).append(set_id)
    poses = list(dict.fromkeys(json.dumps(t["pose"], separators=(",", ":")) for group in packet["sets"] for t in group["takes"]))
    lines = [f"# {packet['round_id']} · {packet['program']}",
             f"Measured: poses {'; '.join(poses)}; level: {json.dumps(packet['level'])}",
             f"Applied: candidate {str(packet['applied']['candidate'] or '')[:12]} · record {packet['applied']['record']} · "
             f"{json.dumps(packet['applied']['layers'], separators=(',', ':'))}",
             f"Result: {packet['result']}; reason: {packet['reason']}",
             "## Decisions"]
    commissioning = packet.get("commissioning") or {}
    if commissioning.get("candidate_fingerprint"):
        lines.insert(4, f"commissioning: apply {commissioning['candidate_fingerprint']} to finish")
    if commissioning.get("status") == "alignment_unmeasured":
        lines.insert(4, f"alignment_unmeasured: {commissioning['reason']}")
    lines += [f"{name}: " + "; ".join(f"sets {', '.join(ids)}: {summary}" for summary, ids in values.items())
              for name, values in decisions.items()]
    lines += ["Limits: packet.json limits is keyed by set; it includes per-bin bounds and admitted features.",
              "Stats: dB from each series reference; tilt over 100–10000 Hz; band means use octave centers in Hz.",
              "Low-end means: power means in Hz ranges; null means no usable bins."]
    takes = {(group["set_id"], take["take_id"], take["role"]): take for group in packet["sets"] for take in group["takes"]}
    by_role: dict[str, list[Mapping[str, Any]]] = {}
    for (_set_id, _take_id, role), take in takes.items():
        by_role.setdefault(role, []).append(take)
    for role, rows in by_role.items():
        lines.append(f"gate {role}: " + "; ".join(
            f"{label} {_span(rows, key)}" for label, key in (
                ("window ms", "gate_window_ms"), ("validity floor Hz", "validity_floor_hz"),
                ("trusted floor Hz", "trusted_floor_hz"), ("source", "floor_source")))
            + f" ({len(rows)} takes)")
    for series in packet["series"]:
        take = takes.get((series["set_id"], series["take_id"], series["role"]), {})
        stats = []
        for name, rows in series["stats"].items():
            for label, row in ([(name, rows)] if "value" in rows else [(f"{name}[{key}]", value) for key, value in rows.items()]):
                mark = f" (below trusted floor {take.get('trusted_floor_hz')} Hz)" if row["below_trusted_floor"] else ""
                stats.append(f"{label}={json.dumps(row['value'])}{mark}")
        lines.append(f"{series['set_id']} / {series['take_id']} / {series['role']}: " + "; ".join(stats))
    lines += [f"Fits: {len(packet['fits'])}",
              "## Artifacts", f"{json.dumps(packet['artifacts'], separators=(',', ':'))}; packet: {PACKET_FILENAME}",
              "## Tools", "\n".join(f"- `{cmd}`" for cmd in dict.fromkeys(commands)),
              f"Fingerprint: {packet['packet_fingerprint']}"]
    return "\n\n".join(lines) + "\n"
