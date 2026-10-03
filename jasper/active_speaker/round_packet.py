# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The saved answer to a completed measurement run."""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Mapping

from jasper.platform.atomic_io import atomic_write_json
from jasper.audio_measurement.evidence_reasons import REASON_UNREADABLE, EvidenceUnavailable, unavailable
from jasper.audio_measurement.series_stats import series_stats
from jasper.audio_measurement.timing_verification import timing_next_action

from .applied_identity import applied_identity
from jasper.audio_measurement.program_analysis.model import TIMING_MEASURED, TIMING_NEEDS_MEASUREMENT
from .alignment_evidence import commissioning_alignment, round_alignment
from .baseline_profile import applied_layer_names
from .crossover_v2.evidence_packet import EVIDENCE_KEY, build_round_evidence, fingerprinted
from .crossover_v2.intervention import CloudFitTerms
from .crossover_v2.position_cycle import OWN_WINDOW, take_curve
from .crossover_v2.prescription_contract import contract_programs, prescription_contracts
from .crossover_v2.round_inputs import (
    INDEX_FILENAME, PACKET_FILENAME, PICTURE_FILENAME, ROUND_PACKET_SCHEMA, RoundInputs, SetTakes, files_by_set,
    round_inputs, prescription_sources, ROUND_INPUT_ERRORS, with_records,
)
from .frequency_plot import prepare_plot_curve
from .frequency_view import FREQUENCY_VIEW_FILENAME
from .linearization_fit import unavailable_fit
from .round_view_artifacts import ARTIFACT_BY_VIEW, PACKET_FAMILIES
from .speaker_fit import design_clouds, speaker_fit
from .measurement_programs import PURPOSE_REAR, PURPOSE_SPEAKER, run_purpose
from .round_verdicts import round_verdicts
from .round_packet_report import gate_fields, packet_index
from .run_manifest import RUN_MANIFEST_KIND, RunManifest, view_sets
from .crossover_v2.refusal_copy import CrossoverV2Refused, exception_detail

if TYPE_CHECKING:
    from .round_bank import BankedRound


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
                merged["takes"].extend(group["takes"])
        first = runs[0] if runs else self.manifest.to_dict()
        measured = any(take["selected"] for group in sets.values() for take in group["takes"])
        issues = [{"code": run["reason"] or next((row["reason"] for row in run["not_measured"]), "take_incomplete"),
                   "blocking": False, "evidence": {"run_id": run["run_id"], "level": run["level"],
                                                    "status": run["status"], "not_measured": run["not_measured"]}}
                  for run in runs if run["finalized"] and run["status"] != "complete"]
        return {**first, "run_id": self.manifest.run_id, "sets": list(sets.values()),
                "schedule": {**self.schedule, "issues": [*self.schedule.get("issues", ()), *issues]},
                "runs": [{key: run[key] for key in ("run_id", "level", "status", "reason", "not_measured", "request_fingerprint")}
                         for run in runs],
                "finalized": self.finalized,
                "status": "complete" if self.finalized and measured else "partial",
                "reason": self.manifest.reason or (issues[0]["code"] if issues and not measured else ""),
                "honoured": {**first["honoured"], **{
                    key: sum(run["honoured"][key] for run in runs)
                    for key in ("mic_moves", "stops_planned", "takes_measured", "takes_refused", "retakes")}},
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

    async def update_schedule(self, schedule: Mapping[str, Any]) -> None:
        self.schedule = dict(schedule)
        self.manifest.path = await self.manifest.records.bank(self.to_dict())


def banked_evidence(inputs: RoundInputs) -> tuple[dict[str, Any], Exception | None]:
    """The evidence a bank stores in ``packet.json``, built once from the round's
    inputs (ADR-0371), and the error when that build failed. A failed build
    stores ``evidence: None``, which readers refuse (ADR-0383)."""
    try:
        evidence = build_round_evidence(inputs)
    except (*ROUND_INPUT_ERRORS, EvidenceUnavailable) as exc:
        return {"packet_fingerprint": None, EVIDENCE_KEY: None}, exc
    return {"packet_fingerprint": evidence.get("packet_fingerprint"), EVIDENCE_KEY: fingerprinted(evidence)}, None


def store_banked_evidence(round_dir: Path) -> Exception | None:
    """Store a round's evidence in its ``packet.json`` when a bank other than
    :func:`write_round_packet` banks it, keeping what the file already holds.
    It names its round as that bank does: a packet's bass evidence binds on ``round_id``.
    A file it creates takes this build's ``schema``; one another bank wrote keeps its own."""
    stored, error = banked_evidence(round_inputs(round_dir))
    path = round_dir / PACKET_FILENAME
    packet = json.loads(path.read_text()) if path.is_file() else {}
    atomic_write_json(path, {"schema": ROUND_PACKET_SCHEMA, "round_id": round_dir.name, **packet, **stored})
    return error


def finish_bass_packet(round_dir: Path, manifest_path: Path, *, join_levels: Callable[..., Path]) -> Path:
    destination = round_dir / PACKET_FILENAME
    manifest = json.loads(manifest_path.read_text())
    if len({run["level"]["run"]["level_db"] for run in manifest.get("runs", ())}) < 2:
        return destination
    manifest = with_records(round_inputs(round_dir).session_dir, manifest, disclose=True)
    candidates = sorted({row["capture_basis"]["candidate_id"] for row in view_sets(manifest) if not row["base"]})
    try:
        table_path = join_levels([round_dir], candidates=[Path(candidate) for candidate in candidates])
        table = json.loads(table_path.read_text())
    except (CrossoverV2Refused, OSError, ValueError, KeyError) as exc:
        table = {**unavailable(_refusal_code(exc, "bass_fit_inputs_missing")), "error_type": type(exc).__name__}
    packet = json.loads(destination.read_text())
    packet["bass_table"] = table
    atomic_write_json(destination, packet)
    (round_dir / INDEX_FILENAME).write_text(packet_index(packet, round_dir, manifest))
    return destination


def _refusal_code(exc: Exception, fallback: str) -> str:
    return getattr(exc, "reason", None) or getattr(exc, "code", None) or fallback


def _fits(inputs: RoundInputs, manifest: Mapping[str, Any], sources: Mapping[str, Any],
          clouds: Mapping[str, CloudFitTerms],
          refused: Mapping[str, EvidenceUnavailable] = MappingProxyType({})) -> list[dict[str, Any]]:
    """Each selected driver take's fit; a take in a set whose design cloud
    ``refused`` names fits nothing, in any of its sets, and carries that code."""
    unclouded = {take["take_id"]: refused[group["set_id"]] for group in manifest.get("sets", ())
                 if group["set_id"] in refused for take in group["takes"]}
    computed: dict[str, Any] = {}
    fits = []
    for group in manifest.get("sets", ()):
        set_role = group["capture_basis"].get("role")
        if set_role in (None, "summed"):
            continue
        for take in group["takes"]:
            if not take["selected"] or take.get("gating_applied") is False:
                continue
            take_id = take["take_id"]
            if take_id in unclouded:
                computed[take_id] = {set_role: {"fit": unavailable_fit(set_role, unclouded[take_id].reason)}}
            elif take_id not in computed or set_role not in computed[take_id]:
                try:
                    result = speaker_fit(inputs, manifest, group["set_id"], take_id,
                                         clouds_by_set=clouds, sources=sources)
                    computed[take_id] = {role: {**proposal, "trim_decision": result["trim_decision"]}
                                         for role, proposal in result["linearization"].items()}
                except ROUND_INPUT_ERRORS + (EvidenceUnavailable,) as exc:
                    computed[take_id] = {set_role: {"fit": unavailable_fit(
                        set_role, _refusal_code(exc, exception_detail(exc)))}}
            for role, proposal in computed[take_id].items():
                if role != set_role:
                    continue
                fit = proposal["fit"]
                trims = (proposal.get("trim_decision") or {}).get("committed_db", {})
                fits.append({"set_id": group["set_id"], "take_id": take_id, "pose": take["pose"], "role": role,
                             "resolved_trim_db": trims,
                             **{key: proposal.get(key) for key in ("boost_evidence", "per_filter_boost_cap_db", "composed_boost_cap_db", "handover_level_shift_db")},
                             **{key: fit.get(key) for key in ("mic_tier", "budget", "filters", "residual_rms_db",
                                                            "residual_max_db", "fit_band_hz", "reason_summary", "position_spread_db", "class_prior_hz")}})
    return fits


def _packet_takes(group: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A set's takes as the packet carries them, each read with its record; only a kept
    take's curve is read, for its gate. A fault is the verdict's, else the capture's incident (ADR-0395)."""
    role = SetTakes.from_row(group).role
    return [{**{key: take.get(key) for key in ("take_id", "pose", "selected", "cleared_layers")}, "role": role,
             "alignment": (take.get("level") or {}).get("alignment"), "screens": verdict.get("screens", []),
             "fault": verdict.get("fault") or take.get("incident") or None,
             **({"record": take["record"]} if "record" in take else {}),
             **gate_fields(take_curve(take, role, OWN_WINDOW) if take["selected"] else None)}
            for take in group["takes"] for verdict in [take.get("verdict") or {}]]


def write_round_packet(target: Path, manifest_path: str | None, views: list[dict[str, Any]]) -> dict[str, Any]:
    inputs = round_inputs(target)
    # A record that cannot be read is disclosed, never the reason a round is not banked.
    manifest = with_records(inputs.session_dir, json.loads(Path(manifest_path).read_text()),
                            disclose=True, every_take=True) if manifest_path else {}
    purpose = run_purpose(manifest.get("preset"))
    errors: list[dict[str, Any]] = []
    series: list[dict[str, Any]] = []
    artifacts: dict[str, Any] = {"frequency_png": None, "frequency_view": None,
                               **{f"{family}_views": [] for family in PACKET_FAMILIES}, "manifest": manifest_path}
    view_path = target / FREQUENCY_VIEW_FILENAME
    try:
        if view_path.is_file():
            view = json.loads(view_path.read_text())
            rows: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = [(g, t) for g in manifest.get("sets", ()) for t in g["takes"]]
            for run_doc in view["runs"]:
                for curve in run_doc["series"]:
                    plot = curve.get("plot") or prepare_plot_curve(curve, run_doc.get("metadata"))
                    curve["plot"] = plot
                    group, take = next(((g, t) for g, t in rows if t["take_id"] == curve.get("take_id")
                                        and (not curve.get("set_id") or g["set_id"] == curve["set_id"])), ({}, {}))
                    gates = gate_fields(curve)
                    series.append({"set_id": curve.get("set_id", group.get("set_id")), "take_id": curve.get("take_id"),
                                   "selected": bool(curve.get("selected")),
                                   "candidate_id": curve.get("candidate_id") or group.get("capture_basis", {}).get("candidate_id"),
                                   "pose": take.get("pose", curve.get("position")), "role": curve.get("role"),
                                   "window": curve["window"],
                                   **gates, "stats": series_stats(curve, plot, gates["trusted_floor_hz"])})
            atomic_write_json(view_path, view)
            artifacts["frequency_view"] = str(view_path)
    except ROUND_INPUT_ERRORS + (ImportError, EvidenceUnavailable) as exc:
        errors.append({"artifact": "frequency", "reason": getattr(exc, "reason", "frequency_unavailable")})
    if (target / PICTURE_FILENAME).is_file():
        artifacts["frequency_png"] = str(target / PICTURE_FILENAME)
    analysis: dict[str, list[dict[str, Any]]] = {family: [] for family in PACKET_FAMILIES}
    for row in views:
        if (spec := ARTIFACT_BY_VIEW.get(row["view"])) is None or spec.packet is None:
            continue
        artifacts[f"{spec.packet}_views"].append({key: row[key] for key in ("view", "set_id", "out", "status", "reason") if key in row})
        if row["view"] == spec.packet and row["status"] == "written" and row.get("out"):
            try:
                document = json.loads(Path(row["out"]).read_text())
            except (OSError, ValueError):
                continue
            if row["view"] == "room":
                document.pop("limits", None)
            analysis[spec.packet].append({**document, "out": row["out"],
                                     "set_id": row.get("set_id") or view_sets(manifest)[0]["set_id"]})
    try:
        sources = prescription_sources(inputs)
    except ROUND_INPUT_ERRORS:
        sources = {}
    profile = sources.get("applied_profile") or {}
    limits = {}
    sets = view_sets(manifest)
    for group in sets:
        try:
            section_sources = prescription_sources(inputs, set_id=group["set_id"] if files_by_set(sets) else None)
            if purpose in contract_programs(section_sources):
                contract = prescription_contracts(programs=(purpose,), **section_sources)[purpose]
                limits[group["set_id"]] = {key: value for key, value in contract.items() if key != "evidence_declarations"}
        except ROUND_INPUT_ERRORS as exc:
            limits[group["set_id"]] = unavailable(_refusal_code(exc, REASON_UNREADABLE))
    stored, error = banked_evidence(inputs)
    if error is not None:
        errors.append({"artifact": EVIDENCE_KEY, "reason": getattr(error, "reason", "evidence_unavailable"),
                       **({"detail": error.detail} if isinstance(error, EvidenceUnavailable) else {})})
    refused: dict[str, EvidenceUnavailable] = {}
    clouds = design_clouds(manifest, refused=refused)
    errors += [{"artifact": "design_clouds", "set_id": set_id, "reason": refusal.reason, "detail": refusal.detail}
               for set_id, refusal in sorted(refused.items())]
    alignments, alignment_verdict = round_alignment(
        {**manifest, "round_id": target.name}, sources,
    ) if purpose == PURPOSE_SPEAKER else ([], None)
    axis = commissioning_alignment(alignments) or {}
    packet = {"schema": ROUND_PACKET_SCHEMA, "round_id": target.name, "run_id": manifest.get("run_id"),
              "result": manifest.get("status"), "reason": manifest.get("reason"),
              "preset": manifest.get("preset"), "layout": manifest.get("layout"), "level": manifest.get("level"),
              "prescriptions": sources.get("candidate", {}).get("analysis", {}).get("evidence", {}).get("prescriptions", {}),
              **({"runs": manifest["runs"]} if "runs" in manifest else {}),
              "applied": {**(applied_identity(profile) or {}), "layers": applied_layer_names(profile)},
              "sets": [{"set_id": g["set_id"], "candidate_id": g["capture_basis"].get("candidate_id"), "base": g.get("base", False),
                        "takes": _packet_takes(g)} for g in manifest.get("sets", ())], "series": series,
              # A fit is gated speaker evidence; a rear take is measured ungated
              # below the gate's trusted floor and proposes no driver filters.
              "fits": [] if purpose == PURPOSE_REAR else _fits(inputs, manifest, sources, clouds, refused),
              **analysis,
              "alignment": alignments, "alignment_verdict": alignment_verdict,
              "next_action": timing_next_action(alignment_verdict or {},
                  measured=axis.get("timing_verdict") == TIMING_MEASURED,
                  needs_measurement=axis.get("timing_verdict") == TIMING_NEEDS_MEASUREMENT),
              **stored, "limits": limits, "artifacts": artifacts, "unavailable": errors}
    if packet["fits"] or purpose == PURPOSE_SPEAKER:
        packet["verdicts"] = round_verdicts(packet, manifest=manifest, clouds=clouds, sources=sources)
    atomic_write_json(target / PACKET_FILENAME, packet)
    (target / INDEX_FILENAME).write_text(packet_index(packet, target, manifest))
    return packet


def wait_answer(banked: BankedRound, result: Mapping[str, Any], *, verbose: bool) -> dict[str, Any]:
    packet_path = banked.path / PACKET_FILENAME
    packet = json.loads(packet_path.read_text()) if packet_path.is_file() else {}
    return {"result": result.get("result"), "reason": result.get("reason") or packet.get("reason"), "round_dir": str(banked.path),
            "packet": str(banked.path / PACKET_FILENAME),
            "picture": str(banked.path / PICTURE_FILENAME) if (banked.path / PICTURE_FILENAME).is_file() else None,
            **({"views": banked.provenance.get("views", [])} if verbose else {})}
