# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Run evidence and selection over immutable capture records (ADR-0015/0017)."""

from __future__ import annotations

import json
from collections.abc import Collection, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Awaitable, Callable, Mapping

from jasper.platform.atomic_io import read_json_mapping
from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.platform.json_fields import finite_float
from jasper.audio_measurement.program import KIND_SWEEP, KIND_SUMMED_SWEEP
from jasper.platform.speaker_layout import measurement_target_parts

from .commissioning_evidence_store import EVIDENCE_ROOT
from .crossover_v2.measure_spec import MeasureSpec
from .crossover_v2.measurement_context import capture_basis
from .crossover_v2.record_index import Measurement, measurement_documents, take_purpose
from .crossover_v2.refusal_copy import TakeVerdict
from .crossover_v2.session_seams import RecordStore
from .measurement_programs import POSE_KIND_CLOSE, BASE_CANDIDATE, candidate_identity

RUN_MANIFEST_KIND = "jts_run_manifest"
RUN_MANIFEST_FILENAME = "run_manifest.json"
TAKE_MEASURED = "measured"
TAKE_INCOMPLETE = "incomplete"
#: A take's own verdict and its level observation, judged on its record before it banks (ADR-0383).
TakeJudge = Callable[[Mapping[str, Any]], Awaitable[tuple[TakeVerdict, Mapping[str, Any]]]]


def _played_basis(record: Mapping[str, Any], role: str | None = None) -> dict[str, Any]:
    basis = capture_basis(record)
    gains = [segment["gain_db"] for segment in (record.get("program") or {}).get("segments", [])
             if segment.get("kind") in {KIND_SWEEP, KIND_SUMMED_SWEEP}
             and (role is None or (segment.get("role") or "summed") == role)]
    if gains:
        # The composer can cap the requested rung; report the emitted sweep gain.
        basis["stimulus_dbfs"] = max(gains)
    return basis


def _take_level(basis: Mapping[str, Any], observed: Mapping[str, Any]) -> dict[str, Any]:
    return {**{key: basis.get(key) for key in ("level_db", "stimulus_dbfs", "stimulus_id")},
            **{key: observed.get(key) for key in ("loudest_half_second_db_spl", "level_delta_db")}}


def kept_measurements(
    bundle_dir: Path, *, phases: Collection[str], purposes: Collection[str],
) -> Iterator[tuple[Measurement, Mapping[str, Any]]]:
    """The takes of these phases and purposes that the round kept, in path order.

    A kept take is one its verdict accepted and its run manifest selected for
    its stop, so a refused take, or one a retake or redo replaced, is never
    read. A bundle with no run manifest keeps none, and a kept take that names
    no purpose refuses (#2902).
    """
    kept = _kept_record_ids(Path(bundle_dir))
    for row, document in measurement_documents(bundle_dir):
        if row.phase in phases and row.path in kept and take_purpose(row, document) in purposes:
            yield row, document


def _kept_record_ids(bundle_dir: Path) -> frozenset[str]:
    manifests = (bundle_dir / EVIDENCE_ROOT / "artifacts").glob(f"crossover_v2/*/{RUN_MANIFEST_FILENAME}")
    return frozenset(
        take["artifacts"]["record_id"]
        for path in manifests
        for group in (read_json_mapping(path) or {}).get("sets", ())
        for take in group["takes"]
        if take["selected"] and take["quality"]["status"] == TAKE_MEASURED
    )


def view_sets(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [row for row in manifest.get("sets", ()) if isinstance(row, Mapping)
            and isinstance(row.get("set_id"), str) and row.get("capture_basis", {}).get("graph_scope") != "timing"]


def room_sets(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [row for row in view_sets(manifest) if row["capture_basis"].get("gating_applied") is False
            and row["capture_basis"].get("role") in (None, "summed")]


#: dB two drivers of one role (so of one declared size) may play apart for the
#: same drive, each measured close to its own cone, before a round shows it.
#: Matched drivers sit well inside it; jts3's two woofers of one model play
#: 6.2 dB apart (#5714).
LEVEL_MISMATCH_DB = 3.0


def driver_level_mismatches(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each near-field microphone position (a close pose less its driver, which
    ``pose_place`` counts once per driver) where the drivers of one role play more
    than :data:`LEVEL_MISMATCH_DB` apart for the same drive. A driver's
    ``unit_drive_db_spl`` is the median, over its kept takes, of the level its
    located sweeps read (ADR-0364) less the stimulus gain and the fader it played
    at. Only close poses compare: from one far bearing a rear-facing driver also
    reads its own off-axis loss and the cabinet's shadow. A finding, never a
    refusal (#5714)."""
    heard: dict[tuple[str, str], dict[str, list[float]]] = {}
    for take in (take for group in view_sets(manifest) for take in group["takes"] if take.get("selected")):
        pose, level = take.get("pose") or {}, take.get("level") or {}
        spl, gain, fader = (finite_float(value) for value in (
            ((take.get("quality") or {}).get("evidence") or {}).get("level_db_spl"),
            level.get("stimulus_dbfs"), level.get("level_db")))
        if (pose.get("driver") and pose.get("kind") == POSE_KIND_CLOSE
                and spl is not None and gain is not None and fader is not None):
            place = json.dumps({key: value for key, value in pose.items() if key not in {"driver", "place"}},
                               sort_keys=True)
            heard.setdefault((measurement_target_parts(pose["driver"])[0], place), {}).setdefault(
                pose["driver"], []).append(spl - gain - fader)
    findings = []
    for (role, place), by_driver in heard.items():
        levels = {driver: median(values) for driver, values in sorted(by_driver.items())}
        if (spread := max(levels.values()) - min(levels.values())) > LEVEL_MISMATCH_DB:
            findings.append({"role": role, "pose": json.loads(place), "spread_db": round(spread, 2),
                             "unit_drive_db_spl": {driver: round(db, 2) for driver, db in levels.items()}})
    return findings


def capture_alignment_levels(
    evidence: Mapping[str, Any], previous: Mapping[str, Any],
) -> dict[str, Any]:
    levels: dict[str, Any] = {}
    for key, value in evidence.items():
        if key.startswith("alignment."):
            _, role, field = key.split(".", 2)
            levels.setdefault(role, {})[field] = value
    for role, level in levels.items():
        shortfall = level.get("alignment_snr_shortfall_db")
        prior = previous.get(role) or {}
        level["alignment_snr_shortfall_db"] = {
            "before": (prior.get("alignment_snr_shortfall_db") or {}).get("before", shortfall),
            "after": shortfall,
        }
        if shortfall is not None and shortfall > 0 and "alignment_level_capped_by" not in level and "alignment_level_capped_by" in prior:
            level["alignment_level_capped_by"] = prior["alignment_level_capped_by"]
            level["alignment_snr_residual_shortfall_db"] = shortfall
    return levels


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
    layout: str = ""
    request_fingerprint: str = ""
    asked: dict[str, Any] = field(default_factory=dict)
    level: dict[str, Any] = field(default_factory=dict)
    planned: list[dict[str, Any]] = field(default_factory=list)
    specs: dict[int, MeasureSpec] = field(default_factory=dict, repr=False)
    mic_moves: int = 0
    spl_monitor: str = ""
    wall_s: list[float] = field(default_factory=list)
    reason: str = ""
    detail: str = ""
    #: The drivers the stopping verdict names (``channel_map_mismatch``).
    failed_roles: tuple[str, ...] = ()
    stopped_at: Mapping[str, int] | None = None
    cancelled: bool = False
    finalized: bool = False
    path: str = ""
    pending_records: list[tuple[Mapping[str, Any], str]] = field(default_factory=list, repr=False)
    judge: TakeJudge | None = field(default=None, init=False, repr=False)
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

    def begin(self, stop: Mapping[str, Any], *, attempt: int, pose_index: int, replay: bool = False) -> None:
        self.pending_records.clear()
        self._attempts += 1
        self._context = {**stop, "attempt": attempt, "pose_index": pose_index, **({"replay": True} if replay else {})}

    def discard_pose(self, pose_index: int) -> None:
        """A redo's pose keeps its takes banked, but none stays kept or a level reference (#5722)."""
        discarded = {take["take_id"] for take in self.takes if take["pose_index"] == pose_index}
        self._chosen = {key: take_id for key, take_id in self._chosen.items() if take_id not in discarded}

    def mark_not_measured(self, index: int, reason: str) -> None:
        next(stop for stop in self.planned if stop["index"] == index)["reason"] = reason

    def allocate_take_id(self) -> str:
        self._ordinal += 1
        return f"{self.run_id}_take_{self._ordinal:04d}"

    def capture_record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        pose = self._context["pose"]
        planned = {
            "preset": self.program, "layout": self.layout,
            "pose": {"driver": None, **{key: value for key, value in pose.items() if key != "place"}},
            "pose_kind": pose["kind"], "mark_distance_m": pose.get("distance_m"),
            "seat_offset_m": pose.get("seat_offset_m"), "pose_driver": pose.get("driver"),
            "measurement_purpose": self._context["purpose"], "purposes": list(self._context["purposes"]),
        }
        context: dict[str, Any] = {key: self._context[key] for key in ("index", "attempt", "repeat", "capture_index")
                                   if key in self._context}
        return {**planned, **record, **context}

    async def bank(self, record: Mapping[str, Any]) -> str:
        from .crossover_v2.capture_provenance import finite_json  # lazy: it loads the analysis stack

        payload = self.capture_record(record)
        assert self.judge is not None
        verdict, observed = await self.judge(payload)
        payload.update(finite_json({"verdict": asdict(verdict), "level": _take_level(_played_basis(payload), observed)}))
        record_id = await self.records.bank(payload)
        self.pending_records.append((payload, record_id))
        return record_id

    def level_observation(self, record: Mapping[str, Any]) -> dict[str, Any]:
        observed = finite_float(((record.get("capture_integrity") or {}).get("spl") or {}).get("loudest_half_second_db_spl"))
        if self._context["pose"].get("driver"):
            # A take at one driver's pose answers to its level target, never its repeats (ADR-0361).
            return {"loudest_half_second_db_spl": observed, "level_reference_db_spl": None, "same_pose": False}
        basis = capture_basis(record)
        gain, program = basis.get("level_db"), basis.get("stimulus_id")
        candidate = self._context.get("candidate_id")
        # Offsets change the gain, each program composes its own stimulus level, and
        # each candidate graph has its own sensitivity; only repeats of this program
        # on this graph at this fader share an expected SPL.
        chosen = set(self._chosen.values())
        accepted = {take["take_id"]: take for take in self.takes
                    if take["take_id"] in chosen
                    and take["level"].get("level_db") == gain
                    and take["level"].get("stimulus_id") == program
                    and take.get("candidate_id") == candidate
                    and take["level"]["loudest_half_second_db_spl"] is not None}
        same = [take for take in accepted.values() if take["pose"] == self._context["pose"]]
        reference = [take["level"]["loudest_half_second_db_spl"] for take in (same or list(accepted.values()))]
        return {"loudest_half_second_db_spl": observed,
                "level_reference_db_spl": median(reference) if reference else None, "same_pose": bool(same)}

    async def append(
        self, record: Mapping[str, Any], record_id: str, verdict: TakeVerdict, *,
        complete: bool, started_s: float, ended_s: float, level_observation: Mapping[str, Any], ordinal: int = 0,
    ) -> None:
        record = {"candidate_id": self._context.get("candidate_id"), **record}
        curves = {curve["role"]: curve for curve in record.get("curves", [])}
        sweeps = [segment for segment in (record.get("program") or {}).get("segments", [])
                  if segment.get("kind") in {KIND_SWEEP, KIND_SUMMED_SWEEP}]
        roles = (set(curves) | {str(segment.get("role") or "summed") for segment in sweeps}) or {record.get("role", "summed")}
        take_id = str(record["take_id"])
        status = TAKE_MEASURED if complete and verdict.ok else TAKE_INCOMPLETE if not complete else "refused"
        previous = max((take for take in self.takes if take["index"] == self._context["index"]
                        and take["stimulus_ordinal"] == ordinal and take.get("alignment")),
                       key=lambda take: take["attempt"], default={}).get("alignment", {})
        alignment = capture_alignment_levels(verdict.evidence, previous)
        for role in sorted(roles):
            basis = _played_basis(record, role)
            # Pose is an observation axis, never a set boundary (brief §2.4).
            basis.pop("pose_kind", None)
            basis.update(role=role, stimulus=record.get("regime"), calibration=dict(self.calibration))
            set_id = json_fingerprint(basis)
            group = self._sets.setdefault(set_id, {"set_id": set_id, "capture_basis": basis,
                "base": candidate_identity(self._context.get("candidate_id") or "") == BASE_CANDIDATE, "takes": []})
            curve = curves.get(role, {})
            band = curve.get("band_hz")
            if band:
                lower = max(band[0], curve.get("validity_floor_hz") or band[0])
                band = [lower, band[1]] if lower < band[1] else None
            row = {**self._context, "take_id": take_id, "stimulus_ordinal": ordinal,
                   "phase": record["phase"] if "phase" in record else self._context.get("phase"),
                   "side": basis["side"], "role": role,
                   "level": _take_level(basis, level_observation),
                   "analysis": record.get("analysis"), "curve": curve or None, "alignment": alignment,
                   "screens": verdict.screens,
                   "quality": {"status": status,
                               "evidence": verdict.evidence, "capabilities": verdict.capabilities,
                               "usable_band_hz": band},
                   **({"fault": verdict.fault, "next": verdict.next, "charge": verdict.charge}
                      if verdict.next != "accept" or not complete else {}),
                   "next_gain_db": verdict.next_gain_db,
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
            "kind": RUN_MANIFEST_KIND, "schema_version": 2, "run_id": self.run_id,
            "program": self.program, "layout": self.layout, "request_fingerprint": self.request_fingerprint,
            "asked": self.asked, "calibration": dict(self.calibration), "incumbent": dict(self.incumbent),
            "level": self.level,
            "honoured": {"spl_monitor": self.spl_monitor, "mic_moves": self.mic_moves,
                         "stops_planned": self.stops_planned, "takes_measured": self.takes_measured,
                         "takes_refused": len({t["take_id"] for t in self.takes if t["quality"]["status"] != TAKE_MEASURED})},
            "sets": [{**group, "takes": [take | {"selected": take["take_id"] in chosen}
                                         for take in group["takes"]]} for group in self._sets.values()],
            "status": self.status, "finalized": self.finalized,
            "reason": self.reason, "detail": self.detail, "stopped_at": self.stopped_at,
            "not_measured": self.not_measured, "attempts": self.attempts, "wall_s": self.wall_s,
        }
