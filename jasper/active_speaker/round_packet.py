# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The saved answer to a completed measurement run."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from jasper.audio_measurement.spatial_combine import octave_bands_hz
from jasper.atomic_io import atomic_write_json

from .baseline_profile import profile_linearization
from .candidate_bank import CandidateBankRefusal
from .commissioning_experiment import bank_commissioning_experiment
from .crossover_v2.evidence_packet import build_crossover_evidence_packet
from .crossover_v2.prescription_contract import prescription_contracts
from .crossover_v2.round_inputs import RoundInputs, round_inputs, prescription_sources, ROUND_INPUT_ERRORS
from .flat_spec import _power_mean_db
from .frequency_plot import prepare_plot_curve, render_frequency_view
from .frequency_view import build_frequency_view, manifest_frequency_run, FREQUENCY_VIEW_FILENAME
from .speaker_fit import speaker_fit
from .measurement_programs import PURPOSE_SPEAKER, run_purpose
from .round_bank import BankedRound

PACKET_FILENAME = "packet.json"
PICTURE_FILENAME = "frequency.png"
INDEX_FILENAME = "index.md"


def _stats(plot: Mapping[str, Any]) -> dict[str, Any]:
    freqs = np.asarray(plot["freqs_hz"], dtype=float)
    values = np.asarray(plot["deviation_db"], dtype=float)
    valid = np.isfinite(values) & (freqs > 0)
    measured = valid & (freqs >= 100) & (freqs <= 10000)
    bands = {}
    for center, lo, hi in octave_bands_hz(20, 20000):
        band = values[valid & (freqs >= lo) & (freqs < hi)]
        bands[f"{center:g}"] = _power_mean_db(band) if band.size else None
    return {
        "rms_100_10k_db": plot["rms_db"],
        "tilt_db_per_decade": float(np.polyfit(np.log10(freqs[measured]), values[measured], 1)[0])
        if np.unique(freqs[measured]).size >= 2 else None,
        "band_means_db": bands,
        "low_end_means_db": {f"{b['band_hz'][0]}_{b['band_hz'][1]}": b["mean_db"] for b in plot["band_means"]},
    }


def _fits(inputs: RoundInputs, manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    computed: dict[str, Any] = {}
    fits = []
    for group in manifest.get("sets", ()):
        for take in group["takes"]:
            if not take["selected"]:
                continue
            take_id = take["take_id"]
            if take_id not in computed:
                try:
                    computed[take_id] = speaker_fit(inputs, manifest, group["set_id"], take_id)["linearization"]
                except ROUND_INPUT_ERRORS as exc:
                    computed[take_id] = exc
            proposals = computed[take_id]
            if isinstance(proposals, Exception):
                proposals = {take.get("role"): {"fit": {"reason_summary": {
                    "unavailable": getattr(proposals, "reason", "speaker_fit_unavailable"),
                }}}}
            for role, proposal in proposals.items():
                if take.get("role") and role != take["role"]:
                    continue
                fit = proposal["fit"]
                fits.append({"set_id": group["set_id"], "take_id": take_id, "pose": take["pose"], "role": role,
                             **{key: fit.get(key) for key in ("mic_tier", "budget", "filters", "residual_rms_db",
                                                            "residual_max_db", "reason_summary")}})
    return fits


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


def _index(packet: Mapping[str, Any], target: Path, views: list[dict[str, Any]]) -> str:
    commands = [shlex.join(["jasper-round-views", row["view"], str(target), *(["--set", row["set_id"]] if row.get("set_id") else []),
                            *(["--incumbent", row["incumbent_set_id"]] if row.get("incumbent_set_id") else [])])
                for row in views if row["view"] != "frequency"]
    if packet["artifacts"]["frequency_view"]:
        commands.append(shlex.join(["jasper-round-views", "frequency", packet["artifacts"]["frequency_view"],
                                   "--image", str(target / PICTURE_FILENAME)]))
    commands += [shlex.join(["jasper-round-views", "speaker-fit", str(target), "--set", fit["set_id"], "--take", fit["take_id"]])
                 for fit in packet["fits"]]
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
             f"Applied: {json.dumps(packet['applied'], separators=(',', ':'))}",
             f"Result: {packet['result']}; reason: {packet['reason']}",
             "## Decisions"]
    commissioning = packet.get("commissioning") or {}
    if commissioning.get("status") == "awaiting_apply":
        lines.insert(4, f"commissioning: apply {commissioning['candidate_fingerprint']} to finish")
    lines += [f"{name}: " + "; ".join(f"sets {', '.join(ids)}: {summary}" for summary, ids in values.items())
              for name, values in decisions.items()]
    lines += ["Limits: packet.json limits is keyed by set; it includes per-bin bounds and admitted features.",
              "Stats: dB from each series reference; tilt over 100–10000 Hz; band means use octave centers in Hz.",
              "Low-end means: power means in Hz ranges; null means no usable bins.",
              f"Fits: {len(packet['fits'])}",
              "## Artifacts", f"{json.dumps(packet['artifacts'], separators=(',', ':'))}; packet: {PACKET_FILENAME}",
              "## Tools", "\n".join(f"- `{cmd}`" for cmd in dict.fromkeys(commands)),
              f"Fingerprint: {packet['packet_fingerprint']}"]
    return "\n\n".join(lines) + "\n"


def write_round_packet(target: Path, manifest_path: str | None, views: list[dict[str, Any]]) -> dict[str, Any]:
    inputs = round_inputs(target)
    manifest = json.loads(Path(manifest_path).read_text()) if manifest_path else {}
    purpose = run_purpose(manifest.get("program"))
    errors: list[dict[str, Any]] = []
    series: list[dict[str, Any]] = []
    artifacts: dict[str, Any] = {"frequency_png": None, "frequency_view": None,
                               "room_views": [], "bass_views": [], "manifest": manifest_path}
    view_path = target / FREQUENCY_VIEW_FILENAME
    try:
        if purpose == PURPOSE_SPEAKER:
            run = manifest_frequency_run(manifest)
            if run.series:
                atomic_write_json(view_path, build_frequency_view(run))
        if view_path.is_file():
            view = json.loads(view_path.read_text())
            series = []
            rows: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = [(g, t) for g in manifest.get("sets", ()) for t in g["takes"]]
            for run_doc in view["runs"]:
                for curve in run_doc["series"]:
                    plot = curve.get("plot") or prepare_plot_curve(curve, run_doc.get("metadata"))
                    curve["plot"] = plot
                    group, take = next(((g, t) for g, t in rows if t["take_id"] == curve.get("take_id")
                                        and (not curve.get("role") or t.get("role") in (None, curve["role"]))), ({}, {}))
                    series.append({"set_id": curve.get("set_id", group.get("set_id")), "take_id": curve.get("take_id"),
                                   "pose": take.get("pose", curve.get("position")), "role": curve.get("role", take.get("role")),
                                   "stats": _stats(plot)})
            atomic_write_json(view_path, view)
            artifacts["frequency_view"] = str(view_path)
            if purpose == PURPOSE_SPEAKER:
                render_frequency_view(view, target / PICTURE_FILENAME)
        else:
            series = []
    except ROUND_INPUT_ERRORS + (ImportError,) as exc:
        errors.append({"artifact": "frequency", "reason": getattr(exc, "reason", "frequency_unavailable")})
    if (target / PICTURE_FILENAME).is_file():
        artifacts["frequency_png"] = str(target / PICTURE_FILENAME)
    for row in views:
        if row["view"].startswith(("room", "bass")):
            artifacts["room_views" if row["view"].startswith("room") else "bass_views"].append(
                {key: row[key] for key in ("view", "set_id", "out", "status", "reason") if key in row})
    try:
        sources = prescription_sources(inputs)
    except ROUND_INPUT_ERRORS:
        sources = {}
    profile = sources.get("applied_profile") or {}
    snapshot = profile.get("recomposition_snapshot") or {}
    limits = {}
    for group in manifest.get("sets", ()):
        try:
            contracts = prescription_contracts(**prescription_sources(inputs, set_id=group["set_id"] if len(manifest["sets"]) > 1 else None))
            if purpose in contracts:
                limits[group["set_id"]] = {key: value for key, value in contracts[purpose].items()
                                           if key != "evidence_declarations"}
        except ROUND_INPUT_ERRORS as exc:
            limits[group["set_id"]] = {"status": "unavailable", "reason": getattr(exc, "reason", "evidence_unreadable")}
    try:
        fingerprint = build_crossover_evidence_packet(
            inputs.session_dir, round_context=inputs, driver_draft_path=inputs.design_draft_path,
            applied_profile_path=inputs.applied_profile_path, repeat_floor_path=inputs.repeat_floor_path,
            declared_geometry_path=inputs.declared_geometry_path,
        )["packet_fingerprint"]
    except ROUND_INPUT_ERRORS as exc:
        fingerprint = None
        errors.append({"artifact": "packet_fingerprint", "reason": getattr(exc, "reason", "evidence_unavailable")})
    packet = {"schema": "jts_round_packet/1", "round_id": target.name, "run_id": manifest.get("run_id"),
              "result": manifest.get("status"), "reason": manifest.get("reason"),
              "program": manifest.get("program"), "level": manifest.get("level"),
              "applied": {"candidate_fingerprint": profile.get("candidate_fingerprint"), "applied_at": profile.get("applied_at"),
                          "layers": {"driver": profile_linearization(profile),
                                     "room": snapshot.get("room_correction", profile.get("room_correction")),
                                     "bass": snapshot.get("bass_extension")}},
              "sets": [{"set_id": g["set_id"], "candidate_id": g["capture_basis"].get("candidate_id"), "base": g.get("base", False),
                        "takes": [{key: t.get(key) for key in ("take_id", "pose", "role", "selected")} for t in g["takes"]]}
                       for g in manifest.get("sets", ())], "series": series,
              "fits": _fits(inputs, manifest) if purpose == PURPOSE_SPEAKER else [],
              "packet_fingerprint": fingerprint, "limits": limits, "artifacts": artifacts, "unavailable": errors}
    if purpose == PURPOSE_SPEAKER:
        try:
            packet["commissioning"] = bank_commissioning_experiment(target, manifest, sources)
        except ROUND_INPUT_ERRORS + (CandidateBankRefusal,) as exc:
            packet["commissioning"] = {"status": "unavailable", "reason": getattr(exc, "code", "commissioning_candidate_unavailable")}
    atomic_write_json(target / PACKET_FILENAME, packet)
    (target / INDEX_FILENAME).write_text(_index(packet, target, views))
    return packet


def wait_answer(banked: BankedRound, result: Mapping[str, Any], *, verbose: bool) -> dict[str, Any]:
    return {"result": result.get("result"), "reason": result.get("reason"), "round_dir": str(banked.path),
            "packet": str(banked.path / PACKET_FILENAME),
            "picture": str(banked.path / PICTURE_FILENAME) if (banked.path / PICTURE_FILENAME).is_file() else None,
            **({"views": banked.provenance.get("views", [])} if verbose else {})}
