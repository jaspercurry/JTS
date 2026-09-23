# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Gate disclosures, summary numbers and the saved round's readable index."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, Mapping, Sequence

from jasper.audio_measurement.gating import f_trusted_floor_hz
from jasper.json_fields import finite_float

from .bass_table_report import bass_table_markdown, bass_table_rows
from .crossover_v2.round_frequency_view import position_label
from .crossover_v2.round_inputs import SetTakes
from .measurement_programs import POSE_KIND_BEARING

PACKET_FILENAME = "packet.json"
PICTURE_FILENAME = "frequency.png"
INDEX_FILENAME = "index.md"


def gate_fields(take: Mapping[str, Any]) -> dict[str, Any]:
    curve = take.get("curve") or {}
    window = finite_float(curve.get("gate_window_ms"))
    return {**{key: curve.get(key) for key in ("gate_window_ms", "validity_floor_hz", "floor_source")},
            "trusted_floor_hz": finite_float(f_trusted_floor_hz(window / 1000)) if window is not None else None}


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



def _pose_token(pose: Mapping[str, Any]) -> str:
    if pose.get("kind") == "seat":
        return str(pose.get("name") or pose.get("id") or f"seat{tuple(pose.get('seat_offset_m') or ())}")
    return position_label({"position_deg": pose.get("deg"),
                           "vertical_deg": pose.get("elevation_deg")})


def _span(rows: list[Mapping[str, Any]], key: str) -> str:
    values = [row.get(key) for row in rows]
    distinct = sorted({str(value) for value in values})
    if len(distinct) == 1:
        return distinct[0]
    numbers = [value for value in values if isinstance(value, (int, float))]
    if len(numbers) == len(values):
        return f"{min(numbers)}–{max(numbers)}"
    return ", ".join(distinct)


def _short_id(candidate_id: Any) -> str:
    return str(candidate_id or "")[:12]


def rear_lines(entries: Sequence[Mapping[str, Any]], target: Path) -> list[str]:
    """Numbers-only lines for each banked ``rear`` comparison: the band, its
    reference and repeat spread, then one line per candidate naming its
    changed control family, headroom cost and worst pooled regression.
    ``jasper-round-views rear`` reads the same entries back."""
    def number(value: float | None) -> str:
        return f"{value:.1f}" if value is not None else "null"

    lines: list[str] = []
    for entry in entries:
        comparison = entry["comparison"]
        band = comparison["band_hz"]
        band_repr = f"{band[0]:g}–{band[1]:g} Hz" if band else "null"
        reference = comparison["reference"]
        spread = comparison["repeat_spread"]
        spread_repr = spread["reason"] or json.dumps(
            {key: number(value) for key, value in spread["spread_db"].items()}, sort_keys=True)
        lines.append(
            f"rear {_short_id(entry['set_id'])}: band {band_repr} ({comparison['band_source']}); "
            f"reference {reference['kind']} {_short_id(reference['candidate_id'])}; "
            f"level {json.dumps(comparison['level'], sort_keys=True)}; "
            f"repeat_spread {spread_repr}"
        )
        for candidate in entry["candidates"]:
            worst = candidate["across_positions"]["worst_regression"]
            if worst is None:
                worst_repr = "unavailable"
            else:
                exceeds = worst["exceeds_repeat_spread"]
                exceeds_word = "unknown" if exceeds is None else ("yes" if exceeds else "no")
                worst_repr = (f"{worst['position']} {worst['figure']} "
                             f"{number(worst['change_db'])} dB exceeds_repeat_spread={exceeds_word}")
            lines.append(
                f"  {_short_id(candidate['candidate_id'])} {candidate['role']}: "
                f"change_family={candidate['change_family'] or 'none'}; "
                f"headroom_change_db={number(candidate['headroom_change_db'])}; "
                f"worst_regression={worst_repr}"
            )
    lines.append(shlex.join(["jasper-round-views", "rear", str(target)]))
    return lines


def packet_index(
    packet: Mapping[str, Any], target: Path, views: list[dict[str, Any]], manifest: Mapping[str, Any],
) -> str:
    commands = [shlex.join(["jasper-round-views", row["view"], str(target), *(["--set", row["set_id"]] if row.get("set_id") else []),
                            *(["--incumbent", row["incumbent_set_id"]] if row.get("incumbent_set_id") else [])])
                for row in views if row["view"] not in ("frequency", "rear")]
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
        sections = {"decision": limits} if "schema" in limits else limits
        for name, contract in sections.items():
            if isinstance(contract, Mapping) and "schema" in contract:
                decisions.setdefault(name, {}).setdefault(_decision(contract), []).append(set_id)
    poses = list(dict.fromkeys(_pose_token(t["pose"]) for group in packet["sets"] for t in group["takes"]))
    lines = [f"# {packet['round_id']} · {packet['program']}",
             f"Measured: poses {'; '.join(poses)}; level: {json.dumps(packet['level'])}",
             f"Applied: candidate {_short_id(packet['applied']['candidate'])} · record {packet['applied']['record']} · "
             f"{json.dumps(packet['applied']['layers'], separators=(',', ':'))}",
             f"Result: {packet['result']}; reason: {packet['reason']}"]
    for pair in packet.get("alignment", ()):
        parallax = json.dumps(pair["parallax_us"])
        if pair["driver_spacing_source"] == "unknown":
            parallax += " (driver spacing undeclared)"
        lines += [f"timing: {'/'.join(pair['roles'])} · {pair['take_id']} · {_pose_token(pair['pose'])}",
                  f"  {pair['objective']}; delay {pair['committed']['delay_us']} us; polarity {pair['committed']['polarity']}",
                  "  " + "; ".join(f"{key} {pair[key]}" for key in ("residual_rms_db", "margin_db", "repeat_spread_db", "repeat_spread_us", "repeat_count")),
                  f"  parallax {parallax}", f"  refinement_delta_us {pair['refinement_delta_us']}; epsilon_ppm {pair['epsilon_ppm']}; flatness_improvement_db {pair['flatness_improvement_db']}"]
        for role, snr in pair["snr"].items():
            level = pair.get("levels", {}).get(role, {})
            snr_fields = [f"  {role} SNR {snr['verdict']}"]
            if (gain := level.get("alignment_level_db")) is not None:
                snr_fields.append(f"level {gain:.1f} dBFS")
            shortfall = level.get("alignment_snr_shortfall_db") or {}
            if shortfall.get("before") is not None and shortfall.get("after") is not None:
                snr_fields.append(f"shortfall {shortfall['before']:.1f} → {shortfall['after']:.1f} dB")
            if level.get("alignment_level_capped_by"):
                snr_fields.append(f"capped {level['alignment_level_capped_by']}, residual {level['alignment_snr_residual_shortfall_db']:.1f} dB")
            lines.append(", ".join(snr_fields))
    if timing := packet.get("alignment_verdict"):
        saved, verification = timing.get("saved") or {}, timing.get("verification") or {}
        lines.append("saved timing: " + "; ".join(f"{key} {saved.get(key)}" for key in ("delay_us", "polarity", "provenance")))
        lines.append("timing verification: " + "; ".join(
            [*(f"{key} {verification.get(key)}" for key in ("status", "residual_rms_db", "repeat_noise_db", "residual_floor_db")),
             *(f"{code} {', '.join(values)}" for code, values in (verification.get("reasons") or {}).items())]))
    if packet.get("next_action"):
        lines.append("next_action: " + packet["next_action"]["label"])
    lines += list(dict.fromkeys(f"retakes: {take['take_id']} {fault}"
                               for group in packet["sets"] for take in group["takes"]
                               if not take["selected"] and (fault := take["fault"])))
    lines.append("## Decisions")
    commissioning = packet.get("commissioning") or {}
    if commissioning.get("candidate_fingerprint"):
        lines.insert(4, f"commissioning: apply {commissioning['candidate_fingerprint']} to finish")
    if commissioning.get("status") == "alignment_unmeasured":
        lines.insert(4, f"alignment_unmeasured: {commissioning['reason']}")
    lines += [f"{name}: " + "; ".join(f"sets {', '.join(ids)}: {summary}" for summary, ids in values.items())
              for name, values in decisions.items()]
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
        stats = []
        for name, rows in series["stats"].items():
            for label, row in ([(name, rows)] if "value" in rows else [(f"{name}[{key}]", value) for key, value in rows.items()]):
                mark = f" (below trusted floor {series.get('trusted_floor_hz')} Hz)" if row.get("below_trusted_floor") else ""
                stats.append(f"{label}={json.dumps(row['value'])}{mark}")
        lines.append(f"series {series['role']}: pose {_pose_token(series['pose'])}; " + "; ".join(stats)
                     + f"; set {series['set_id']}; take {series['take_id']}"
                     + (f"; window {series['window']}" if series.get("window") else ""))
    lines += [f"crossover_band_spread=null; reason={reason}" for reason in dict.fromkeys(
        fit.get("crossover_band_spread_reason") for fit in packet["fits"]
    ) if reason]
    for fit in packet["fits"]:
        cloud = fit.get("boost_evidence") or {}
        bands = "; ".join("crossover " + " ".join(f"{key}={band[key]:.4g}" for key in ("center_hz", "sigma_db", "max_sigma_db"))
                          for band in (fit["crossover_band_spread"] or {}).values())
        fields = {key: fit.get(key) for key in ("residual_rms_db", "residual_max_db", "reason_summary", "position_spread_db", "class_prior_hz", "resolved_trim_db")}
        fields.update(design_poses=cloud.get("design_poses"), **fit["verdict"], filters=fit["filters"])
        lines.append(f"fit {fit['role']}: pose {_pose_token(fit['pose'])}; " + (bands + "; " if bands else "")
                     + "; ".join(f"{key}={json.dumps(value)}" for key, value in fields.items())
                     + f"; set {fit['set_id']}; take {fit['take_id']}")
    for row in packet.get("verdicts", ()):
        summary = f"unavailable ({row['reason']})"
        if row["branch_gap_db"] is not None:
            lo, hi = row["band_hz"]
            louder = f"{row['louder_role']} louder" if row["louder_role"] else "equal levels"
            ceiling = f"{row['null_ceiling_db']:.4g}" if row['null_ceiling_db'] is not None else "null"
            summary = (f"gap {row['branch_gap_db']:.4g} dB ({louder}) → ceiling {ceiling} dB "
                       f"over {lo:g}–{hi:g} Hz" + (f" ({row['reason']})" if row["reason"] else ""))
        lines.append(f"null ceiling {_pose_token(row['pose'])}: {summary}; capture_graph {json.dumps(row['capture_graph'])}; "
                     f"takes {', '.join(row['take_ids'])}")
    if bass_rows := bass_table_rows(packet.get("bass_table", {})):
        lines.append(bass_table_markdown(bass_rows))
    if rear_entries := packet.get("rear"):
        lines.append("\n".join(rear_lines(rear_entries, target)))
    lines += ["## Artifacts", f"{json.dumps(packet['artifacts'], separators=(',', ':'))}; packet: {PACKET_FILENAME}",
              "## Tools", "\n".join(f"- `{cmd}`" for cmd in dict.fromkeys(commands)),
              f"Fingerprint: {packet['packet_fingerprint']}"]
    return "\n\n".join(lines) + "\n"
