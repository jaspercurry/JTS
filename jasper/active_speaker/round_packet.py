# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The saved answer to a completed measurement run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from jasper.atomic_io import atomic_write_json

from .applied_identity import applied_identity
from .alignment_evidence import round_alignment
from .baseline_profile import profile_linearization
from .candidate_bank import CandidateBankRefusal
from .commissioning_experiment import bank_commissioning_experiment
from .crossover_v2.evidence_packet import build_crossover_evidence_packet
from .crossover_v2.intervention import CloudFitTerms
from .crossover_v2.prescription_contract import prescription_contracts
from .crossover_v2.round_inputs import RoundInputs, round_inputs, prescription_sources, ROUND_INPUT_ERRORS
from .frequency_plot import prepare_plot_curve, render_frequency_view
from .frequency_view import build_frequency_view, manifest_frequency_run, FREQUENCY_VIEW_FILENAME
from .speaker_fit import design_clouds, speaker_fit
from .measurement_programs import PURPOSE_SPEAKER, run_purpose
from .round_bank import BankedRound
from .round_verdicts import round_verdicts
from .round_packet_report import (
    INDEX_FILENAME, PACKET_FILENAME, PICTURE_FILENAME, gate_fields, packet_index, series_stats,
)
from .run_manifest import RUN_MANIFEST_KIND, RunManifest
from .crossover_v2.refusal_copy import CrossoverV2Refused


class RoundPacket:
    def __init__(self, manifest: RunManifest, schedule: Mapping[str, Any]) -> None:
        self.manifest, self.schedule = manifest, schedule
        self.runs: dict[str, Mapping[str, Any]] = {}
        self.finalized = False

    def to_dict(self) -> dict[str, Any]:
        runs = list(self.runs.values())
        sets: dict[str, dict[str, Any]] = {}
        for run in runs:
            for group in run["sets"]:
                merged = sets.setdefault(group["set_id"], {**group, "takes": []})
                merged["takes"].extend({**take, "run_id": run["run_id"]} for take in group["takes"])
        first = runs[0] if runs else self.manifest.to_dict()
        return {**first, "run_id": self.manifest.run_id, "sets": list(sets.values()),
                "schedule": self.schedule,
                "runs": [{key: run[key] for key in ("run_id", "level", "status", "reason", "request_fingerprint")}
                         for run in runs],
                "level": {"session": first["level"].get("session")},
                "finalized": self.finalized,
                "status": "complete" if self.finalized and runs and all(run["status"] == "complete" for run in runs)
                          and not self.manifest.reason else "partial",
                "reason": self.manifest.reason or next((run["reason"] for run in runs if run["reason"]), ""),
                "honoured": {**first["honoured"], **{
                    key: sum(run["honoured"][key] for run in runs)
                    for key in ("mic_moves", "stops_planned", "takes_measured", "takes_refused")}},
                "attempts": sum(run["attempts"] for run in runs),
                "wall_s": [value for run in runs for value in run["wall_s"]],
                "not_measured": [{**take, "run_id": run["run_id"]} for run in runs for take in run["not_measured"]]}

    async def bank(self, record: Mapping[str, Any]) -> str:
        if record.get("kind") == RUN_MANIFEST_KIND:
            self.runs[record["run_id"]] = record
            record = self.to_dict()
        return await self.manifest.records.bank(record)

    async def finish(self) -> None:
        self.finalized = True
        self.manifest.path = await self.manifest.records.bank(self.to_dict())


def finish_bass_packet(round_dir: Path, manifest_path: Path, *, join_levels: Callable[..., Path]) -> Path:
    destination = round_dir / PACKET_FILENAME
    manifest = json.loads(manifest_path.read_text())
    if len({run["level"]["run"]["level_db"] for run in manifest.get("runs", ())}) < 2:
        return destination
    candidates = sorted({row["capture_basis"]["candidate_id"] for row in manifest["sets"] if not row["base"]})
    try:
        table_path = join_levels([round_dir], candidates=[Path(candidate) for candidate in candidates])
        table = json.loads(table_path.read_text())
    except (CrossoverV2Refused, OSError, ValueError, KeyError) as exc:
        table = {"status": "unavailable", "code": getattr(exc, "code", "bass_fit_inputs_missing"),
                 "error_type": type(exc).__name__}
    packet = json.loads(destination.read_text())
    packet["bass_table"] = table
    atomic_write_json(destination, packet)
    (round_dir / INDEX_FILENAME).write_text(packet_index(packet, round_dir, packet["artifacts"]["bass_views"], manifest))
    return destination


def _fits(inputs: RoundInputs, manifest: Mapping[str, Any], sources: Mapping[str, Any],
          clouds: Mapping[str, CloudFitTerms]) -> list[dict[str, Any]]:
    computed: dict[str, Any] = {}
    fits = []
    for group in manifest.get("sets", ()):
        for take in group["takes"]:
            if not take["selected"] or take.get("role") in (None, "summed"):
                continue
            take_id = take["take_id"]
            if take_id not in computed or take["role"] not in computed[take_id]:
                try:
                    computed[take_id] = speaker_fit(inputs, manifest, group["set_id"], take_id,
                                                clouds_by_set=clouds, sources=sources)["linearization"]
                except ROUND_INPUT_ERRORS as exc:
                    computed[take_id] = {take["role"]: {"fit": {"reason_summary": {
                        "unavailable": getattr(exc, "reason", None) or getattr(exc, "code", None) or "speaker_fit_unavailable",
                    }}}}
            for role, proposal in computed[take_id].items():
                if role != take["role"]:
                    continue
                fit = proposal["fit"]
                fits.append({"set_id": group["set_id"], "take_id": take_id, "pose": take["pose"], "role": role,
                             **{key: proposal.get(key) for key in ("boost_evidence", "per_filter_boost_cap_db", "composed_boost_cap_db")},
                             **{key: fit.get(key) for key in ("mic_tier", "budget", "filters", "residual_rms_db",
                                                            "residual_max_db", "reason_summary", "position_spread_db", "class_prior_hz")}})
    return fits


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
                                        and (not curve.get("set_id") or g["set_id"] == curve["set_id"])
                                        and (not curve.get("role") or t.get("role") in (None, curve["role"]))), ({}, {}))
                    series.append({"set_id": curve.get("set_id", group.get("set_id")), "take_id": curve.get("take_id"),
                                   "pose": take.get("pose", curve.get("position")), "role": curve.get("role", take.get("role")),
                                   "stats": series_stats(plot, gate_fields(take)["trusted_floor_hz"])})
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
    analysis: dict[str, list[dict[str, Any]]] = {"room": [], "bass": []}
    for row in views:
        if row["view"].startswith(("room", "bass")):
            artifacts["room_views" if row["view"].startswith("room") else "bass_views"].append(
                {key: row[key] for key in ("view", "set_id", "out", "status", "reason") if key in row})
        if row["view"] == purpose and purpose in analysis and row["status"] == "written" and row.get("out"):
            try:
                document = json.loads(Path(row["out"]).read_text())
            except (OSError, ValueError):
                continue
            if purpose == "room":
                document.pop("limits", None)
            analysis[purpose].append({**document, "out": row["out"],
                                     "set_id": row.get("set_id") or manifest["sets"][0]["set_id"]})
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
    clouds = design_clouds(inputs, manifest) if purpose == PURPOSE_SPEAKER else {}
    corners = {contract.get("alignment", {}).get("bounds", {}).get("fc_hz") for contract in limits.values()}
    corners.discard(None)
    alignments, alignment_verdict = round_alignment(
        manifest, sources, fc_hz=next(iter(corners)) if len(corners) == 1 else None,
    ) if purpose == PURPOSE_SPEAKER else ([], None)
    packet = {"schema": "jts_round_packet/2", "round_id": target.name, "run_id": manifest.get("run_id"),
              "result": manifest.get("status"), "reason": manifest.get("reason"),
              "program": manifest.get("program"), "level": manifest.get("level"),
              **({"runs": manifest["runs"]} if "runs" in manifest else {}),
              "applied": {**(applied_identity(profile) or {}),
                          "layers": {"driver": profile_linearization(profile),
                                     "room": snapshot.get("room_correction", profile.get("room_correction")),
                                     "bass": snapshot.get("bass_extension")}},
              "sets": [{"set_id": g["set_id"], "candidate_id": g["capture_basis"].get("candidate_id"), "base": g.get("base", False),
                        "takes": [{**{key: t.get(key) for key in ("take_id", "pose", "role", "selected", "alignment")},
                                   "fault": t.get("fault") or (t.get("quality") or {}).get("fault"), **gate_fields(t)} for t in g["takes"]]}
                       for g in manifest.get("sets", ())], "series": series,
              "fits": _fits(inputs, manifest, sources, clouds) if purpose == PURPOSE_SPEAKER else [],
              **analysis,
              "alignment": alignments, "alignment_verdict": alignment_verdict,
              "packet_fingerprint": fingerprint, "limits": limits, "artifacts": artifacts, "unavailable": errors}
    if purpose == PURPOSE_SPEAKER:
        try:
            packet["commissioning"] = bank_commissioning_experiment(target, manifest, sources, packet["alignment"])
        except ROUND_INPUT_ERRORS + (CandidateBankRefusal,) as exc:
            packet["commissioning"] = {"status": "unavailable", "reason": getattr(exc, "code", "commissioning_candidate_unavailable")}
        packet["verdicts"] = round_verdicts(packet, inputs, manifest=manifest, clouds=clouds, sources=sources)
    atomic_write_json(target / PACKET_FILENAME, packet)
    (target / INDEX_FILENAME).write_text(packet_index(packet, target, views, manifest))
    return packet


def wait_answer(banked: BankedRound, result: Mapping[str, Any], *, verbose: bool) -> dict[str, Any]:
    return {"result": result.get("result"), "reason": result.get("reason"), "round_dir": str(banked.path),
            "packet": str(banked.path / PACKET_FILENAME),
            "picture": str(banked.path / PICTURE_FILENAME) if (banked.path / PICTURE_FILENAME).is_file() else None,
            **({"views": banked.provenance.get("views", [])} if verbose else {})}
