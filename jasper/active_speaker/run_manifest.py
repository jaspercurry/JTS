# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Run evidence and selection over immutable capture records (ADR-0015/0017)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.audio_measurement.program import KIND_SWEEP, KIND_SUMMED_SWEEP

from .crossover_v2.measure_spec import MeasureSpec
from .crossover_v2.measurement_context import capture_basis
from .crossover_v2.refusal_copy import TakeVerdict
from .crossover_v2.session import MeasureOutcome
from .crossover_v2.session_seams import RecordStore

RUN_MANIFEST_KIND = "jts_run_manifest"
RUN_MANIFEST_FILENAME = "run_manifest.json"
TAKE_MEASURED = "measured"
TAKE_INCOMPLETE = "incomplete"


def incumbent_fingerprints(profile: Mapping[str, Any] | None) -> dict[str, Any]:
    profile = profile or {}
    snapshot = profile.get("recomposition_snapshot") or {}
    return {
        "speaker": (profile.get("source") or {}).get("measured_candidate_fingerprint"),
        **{layer: json_fingerprint(value) if value else None for layer, value in (
            ("room", snapshot.get("room_correction", profile.get("room_correction"))),
            ("bass", snapshot.get("bass_extension", profile.get("bass_extension"))),
        )},
    }


@dataclass
class RunManifest:
    run_id: str
    records: RecordStore
    calibration: Mapping[str, Any] = field(default_factory=lambda: {"id": None, "curve_fingerprint": None})
    incumbent: Mapping[str, Any] = field(default_factory=lambda: {"speaker": None, "room": None, "bass": None})
    program: str = ""
    request_fingerprint: str = ""
    asked: dict[str, Any] = field(default_factory=dict)
    baseline_graph: str | None = None
    planned: list[dict[str, Any]] = field(default_factory=list)
    specs: dict[int, MeasureSpec] = field(default_factory=dict, repr=False)
    outcomes: list[tuple[MeasureOutcome, str]] = field(default_factory=list, repr=False)
    mic_moves: int = 0
    spl_monitor: str = ""
    wall_s: list[float] = field(default_factory=list)
    reason: str = ""
    detail: str = ""
    stopped_at: Mapping[str, int] | None = None
    cancelled: bool = False
    finalized: bool = False
    path: str = ""
    pending_records: list[tuple[Mapping[str, Any], str]] = field(default_factory=list, repr=False)
    _context: dict[str, Any] = field(default_factory=dict, repr=False)
    _ordinal: int = 0
    _attempts: int = 0
    _sets: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _chosen: dict[tuple[int, int], str] = field(default_factory=dict, repr=False)

    @property
    def takes(self) -> list[dict[str, Any]]:
        return [take for group in self._sets.values() for take in group["takes"]]

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def stops_planned(self) -> int:
        return len(self.planned)

    @property
    def takes_measured(self) -> int:
        return len({t["artifacts"]["record_id"] for t in self.takes if t["artifacts"]["record_id"]})

    @property
    def takes_skipped(self) -> int:
        return len(self.not_measured)

    @property
    def not_measured(self) -> list[dict[str, Any]]:
        landed = {index for index, _ordinal in self._chosen}
        missing = {take["index"] for take in self.takes
                   if (take["index"], take["stimulus_ordinal"]) not in self._chosen}
        return [{**stop, "reason": stop.get("reason") or self.reason or "take_incomplete"}
                for stop in self.planned if stop["index"] not in landed or stop["index"] in missing]

    @property
    def status(self) -> str:
        if self.cancelled:
            return "cancelled"
        if not self.finalized or self.reason or not self.takes or self.not_measured:
            return "partial"
        return "complete"

    def begin(self, stop: Mapping[str, Any], *, attempt: int, pose_index: int) -> None:
        self.pending_records.clear()
        self._attempts += 1
        self._context = {**stop, "attempt": attempt, "pose_index": pose_index}

    def allocate_take_id(self) -> str:
        self._ordinal += 1
        return f"{self.run_id}_take_{self._ordinal:04d}"

    async def bank(self, record: Mapping[str, Any]) -> str:
        """Bind inside the host's capture annotation seam, before its raw store."""
        pose = self._context["pose"]
        payload = {**record, "take_id": self.allocate_take_id(),
                   "index": self._context["index"], "attempt": self._context["attempt"],
                   "repeat": self._context["repeat"], "pose_kind": pose["kind"],
                   "mark_distance_m": pose["distance_m"], "seat_offset_m": pose.get("seat_offset_m")}
        record_id = await self.records.bank(payload)
        self.pending_records.append((payload, record_id))
        return record_id

    async def append(
        self, record: Mapping[str, Any], record_id: str, verdict: TakeVerdict, *,
        complete: bool, started_s: float, ended_s: float, ordinal: int = 0,
    ) -> None:
        record = {"candidate_id": self._context.get("candidate_id"), **record}
        curves = {curve["role"]: curve for curve in record.get("curves", [])}
        sweeps = [segment for segment in (record.get("program") or {}).get("segments", [])
                  if segment.get("kind") in {KIND_SWEEP, KIND_SUMMED_SWEEP}]
        roles = (set(curves) | {str(segment.get("role") or "summed") for segment in sweeps}) or {record.get("role", "summed")}
        take_id = str(record.get("take_id") or self.allocate_take_id())
        status = TAKE_MEASURED if complete and verdict.ok else TAKE_INCOMPLETE if not complete else "refused"
        for role in sorted(roles):
            basis = capture_basis(record)
            # Pose is an observation axis, never a set boundary (brief §2.4).
            basis.pop("pose_kind", None)
            basis.update(role=role, stimulus=record.get("regime"), calibration=dict(self.calibration))
            gains = [segment["gain_db"] for segment in sweeps if (segment.get("role") or "summed") == role]
            if gains:
                # The composer can cap the requested rung; report the emitted sweep gain.
                basis["stimulus_dbfs"] = max(gains)
            set_id = json_fingerprint(basis)
            group = self._sets.setdefault(set_id, {"set_id": set_id, "capture_basis": basis, "takes": []})
            curve = curves.get(role, {})
            band = curve.get("band_hz")
            if band:
                lower = max(band[0], curve.get("validity_floor_hz") or band[0])
                band = [lower, band[1]] if lower < band[1] else None
            row = {**self._context, "take_id": take_id, "stimulus_ordinal": ordinal,
                   "side": basis["side"], "role": role,
                   "level": {key: basis.get(key) for key in
                             ("level_db", "stimulus_dbfs", "loudness_volume_db", "program_id")},
                   "analysis": record.get("analysis"),
                   "quality": {"status": status, "fault": verdict.fault,
                               "evidence": verdict.evidence, "capabilities": verdict.capabilities,
                               "usable_band_hz": band},
                   "next_action": verdict.next, "next_gain_db": verdict.next_gain_db, "charge": verdict.charge,
                   "artifacts": {"record_id": record_id, "wav_sha256": record.get("wav_sha256"),
                                 "wav_path": record.get("wav_path")},
                   "timing": {"started_s": started_s, "ended_s": ended_s}}
            group["takes"].append(row)
        if complete and verdict.ok:
            self._chosen[(self._context["index"], ordinal)] = take_id
        await self.persist()

    async def persist(self) -> None:
        self.path = await self.records.bank(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        chosen = set(self._chosen.values())
        return {
            "kind": RUN_MANIFEST_KIND, "schema_version": 1, "run_id": self.run_id,
            "program": self.program, "request_fingerprint": self.request_fingerprint,
            "asked": self.asked, "calibration": dict(self.calibration), "incumbent": dict(self.incumbent),
            "baseline_graph": self.baseline_graph,
            "honoured": {"spl_monitor": self.spl_monitor, "mic_moves": self.mic_moves,
                         "stops_planned": self.stops_planned, "takes_measured": self.takes_measured,
                         "takes_refused": len({t["take_id"] for t in self.takes if t["quality"]["status"] != TAKE_MEASURED})},
            "sets": [{**group, "takes": [take | {"selected": take["take_id"] in chosen}
                                         for take in group["takes"]]} for group in self._sets.values()],
            "status": self.status, "finalized": self.finalized,
            "reason": self.reason, "detail": self.detail, "stopped_at": self.stopped_at,
            "not_measured": self.not_measured, "attempts": self.attempts, "wall_s": self.wall_s,
        }
