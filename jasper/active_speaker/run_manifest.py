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
from jasper.audio_measurement.evidence_reasons import TAKE_CURVES_NOT_BANKED
from jasper.platform.json_fields import finite_float, finite_json
from jasper.audio_measurement.program import KIND_SWEEP, KIND_SUMMED_SWEEP, ExcitationProgram, is_level_probe
from jasper.platform.speaker_layout import measurement_target_parts

from .commissioning_evidence_store import EVIDENCE_ROOT
from .crossover_v2.measure_spec import MeasureSpec
from .crossover_v2.measurement_context import CAPTURE_FIELDS, SHAPE_FIELDS, capture_basis, shaped_capture_basis
from .crossover_v2.record_index import Measurement, measurement_documents, take_purpose
from .crossover_v2.refusal_copy import REASON_NOT_REACHED, TakeVerdict
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
    segments = (record.get("program") or {}).get("segments", [])
    gains = [segment["gain_db"] for segment in segments
             if segment.get("kind") in {KIND_SWEEP, KIND_SUMMED_SWEEP}
             and (role is None or (segment.get("role") or "summed") == role)]
    if gains:
        # The composer can cap the requested level; report the emitted sweep gain.
        basis["stimulus_dbfs"] = max(gains)
    if record.get("program") and is_level_probe(ExcitationProgram.from_dict(record["program"])):
        basis["level_probe"] = True
    return basis


def set_basis(record: Mapping[str, Any], basis: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """What a take's set shares: ``basis`` (the record's capture basis by default) with no
    pose (brief §2.4) and its stimulus named by shape, never by level (ADR-0433)."""
    return {key: value for key, value in shaped_capture_basis(record, basis).items()
            if key != "pose_kind" and (key in SHAPE_FIELDS or key not in CAPTURE_FIELDS)}


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


class RoundSetRefused(ValueError):
    """A round's reader refuses by ``reason``, with ``detail`` naming what it read."""

    def __init__(self, reason: str, **detail: Any) -> None:
        self.reason, self.detail = reason, detail
        super().__init__(reason)


def row_record_id(row: Mapping[str, Any]) -> str:
    """The record a take row points at. A row with none was banked before the
    rows became pointers, and refuses by that field (#2902, ADR-0395)."""
    if "record_id" not in row:
        raise RoundSetRefused(TAKE_CURVES_NOT_BANKED, take_id=row.get("take_id"), field="record_id")
    return str(row["record_id"])


def pointer_rows(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    """``manifest``, once every take row points at its record: one banked before
    the rows became pointers refuses by that field (#2902, ADR-0395 §5)."""
    for group in manifest.get("sets", ()):
        for take in group["takes"]:
            row_record_id(take)
    return manifest


def _kept_record_ids(bundle_dir: Path) -> frozenset[str]:
    manifests = (bundle_dir / EVIDENCE_ROOT / "artifacts").glob(f"crossover_v2/*/{RUN_MANIFEST_FILENAME}")
    return frozenset(
        row_record_id(take)
        for path in manifests
        for group in (read_json_mapping(path) or {}).get("sets", ())
        for take in group["takes"]
        if take["selected"]
    )


def view_sets(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The sets a view reads: neither the timing take's nor a level probe's (ADR-0365)."""
    return [row for row in manifest.get("sets", ()) if isinstance(row, Mapping)
            and isinstance(row.get("set_id"), str) and row.get("capture_basis", {}).get("graph_scope") != "timing"
            and not row.get("capture_basis", {}).get("level_probe")]


#: dB two drivers of one role (so of one declared size) may play apart for the
#: same drive, each measured close to its own cone, before a round shows it.
#: Matched drivers sit well inside it; jts3's two woofers of one model play
#: 6.2 dB apart (#5714).
LEVEL_MISMATCH_DB = 3.0


def driver_level_mismatches(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each near-field microphone position (a close pose less its driver, which
    ``Pose.place`` counts once per driver) where the drivers of one role play more
    than :data:`LEVEL_MISMATCH_DB` apart for the same drive. A driver's
    ``unit_drive_db_spl`` is the median, over its kept takes, of the level its
    located sweeps read (ADR-0364) less the stimulus gain and the fader the take
    played at; the takes are read with their records (ADR-0395). Only close
    poses compare: from one far bearing a rear-facing driver also reads its own
    off-axis loss and the cabinet's shadow. A finding, never a refusal (#5714)."""
    heard: dict[tuple[str, str], dict[str, list[float]]] = {}
    for take in (take for group in view_sets(manifest) for take in group["takes"] if take.get("selected")):
        pose, level = take.get("pose") or {}, take.get("level") or {}
        spl, gain, fader = (finite_float(value) for value in (
            ((take.get("verdict") or {}).get("evidence") or {}).get("level_db_spl"),
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
    preset: str = ""
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
    _begun: set[int] = field(default_factory=set, repr=False)
    _sets: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _chosen: dict[tuple[int, int], str] = field(default_factory=dict, repr=False)
    _banked: dict[str, Mapping[str, Any]] = field(default_factory=dict, repr=False)

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
        return len({t["record_id"] for t in self.takes if t["record_id"]})

    @property
    def takes_skipped(self) -> int:
        return len(self.not_measured)


    @property
    def not_measured(self) -> list[dict[str, Any]]:
        landed = {index for index, _ordinal in self._chosen}
        missing = {take["index"] for take in self.takes
                   if (take["index"], take["stimulus_ordinal"]) not in self._chosen}
        left = [stop for stop in self.planned if stop["index"] not in landed or stop["index"] in missing]
        # The run's stop reason belongs to the stop it halted at. When the run left that stop unmeasured,
        # a stop it never began was not reached. When it halted after a kept take, or before its first,
        # the reason is the only one those stops have. An operator's early Complete waives them.
        halted_at = self.reason not in ("", "complete_requested") and any(
            stop["index"] in self._begun and not stop.get("reason") for stop in left)
        return [{**stop, "reason": stop.get("reason") or (
                    REASON_NOT_REACHED if halted_at and stop["index"] not in self._begun
                    else self.reason or "take_incomplete")}
                for stop in left]

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
        self._begun.add(stop["index"])
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
            "preset": self.preset, "layout": self.layout,
            "pose": {"driver": None, **{key: value for key, value in pose.items() if key != "place"}},
            "pose_kind": pose["kind"], "mark_distance_m": pose.get("distance_m"),
            "seat_offset_m": pose.get("seat_offset_m"), "pose_driver": pose.get("driver"),
            "measurement_purpose": self._context["purpose"], "purposes": list(self._context["purposes"]),
        }
        context: dict[str, Any] = {key: self._context[key] for key in (
            "index", "attempt", "capture_index", "pose_index") if key in self._context}
        return {**planned, **record, **context, "stimulus_ordinal": len(self.pending_records)}

    async def bank(self, record: Mapping[str, Any]) -> str:
        payload = self.capture_record(record)
        assert self.judge is not None
        verdict, observed = await self.judge(payload)
        previous = max((take for take in self.takes if take["index"] == payload.get("index")
                        and take["stimulus_ordinal"] == payload["stimulus_ordinal"] and take.get("alignment")),
                       key=lambda take: take["attempt"], default={}).get("alignment", {})
        payload.update(finite_json({"verdict": asdict(verdict), "level": {
            **_take_level(_played_basis(payload), observed),
            "alignment": capture_alignment_levels(verdict.evidence, previous)}}))
        record_id = await self.records.bank(payload)
        self.pending_records.append((payload, record_id))
        self._banked[record_id] = payload
        return record_id

    def level_observation(self, record: Mapping[str, Any]) -> dict[str, Any]:
        observed = finite_float(((record.get("capture_integrity") or {}).get("spl") or {}).get("loudest_half_second_db_spl"))
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
        complete: bool, level_observation: Mapping[str, Any], ordinal: int = 0,
    ) -> None:
        """One take's row in each role's set. The live row holds the stop and what
        the run decided of the take; the record holds the take (ADR-0395)."""
        record = {"candidate_id": self._context.get("candidate_id"), **record}
        sweeps = [segment for segment in (record.get("program") or {}).get("segments", [])
                  if segment.get("kind") in {KIND_SWEEP, KIND_SUMMED_SWEEP}]
        roles = ({curve["role"] for curve in record.get("curves", [])}
                 | {str(segment.get("role") or "summed") for segment in sweeps}) or {record.get("role", "summed")}
        take_id = str(record["take_id"])
        status = TAKE_MEASURED if complete and verdict.ok else TAKE_INCOMPLETE if not complete else "refused"
        alignment = (record.get("level") or {}).get("alignment") or {}
        for role in sorted(roles):
            played = _played_basis(record, role)
            basis = {**set_basis(record, played), "role": role, "calibration": dict(self.calibration)}
            set_id = json_fingerprint(basis)
            group = self._sets.setdefault(set_id, {"set_id": set_id, "capture_basis": basis,
                "base": candidate_identity(self._context.get("candidate_id") or "") == BASE_CANDIDATE, "takes": []})
            group["takes"].append({
                **self._context, "take_id": take_id, "record_id": record_id, "stimulus_ordinal": ordinal,
                "level": _take_level(played, level_observation), "alignment": alignment, "quality": {"status": status},
                **({"fault": verdict.fault, "next": verdict.next, "charge": verdict.charge}
                   if verdict.next != "accept" or not complete else {})})
        if complete and verdict.ok:
            self._chosen[(self._context["index"], ordinal)] = take_id
        if not record_id:
            # A take whose program never played banks no record and stops the run:
            # its stop, not measured, keeps its fault and evidence (ADR-0395 §7).
            next(stop for stop in self.planned if stop["index"] == self._context["index"]).update(
                fault=verdict.fault, evidence=verdict.evidence)
        await self.persist()

    async def persist(self) -> None:
        self.path = await self.records.bank(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        chosen = set(self._chosen.values())
        return {
            "kind": RUN_MANIFEST_KIND, "schema_version": 5, "run_id": self.run_id,
            "preset": self.preset, "layout": self.layout, "request_fingerprint": self.request_fingerprint,
            "asked": self.asked, "calibration": dict(self.calibration), "incumbent": dict(self.incumbent),
            "level": self.level,
            "honoured": {"spl_monitor": self.spl_monitor, "mic_moves": self.mic_moves,
                         "stops_planned": self.stops_planned, "takes_measured": self.takes_measured,
                         "takes_refused": len({t["take_id"] for t in self.takes if t["quality"]["status"] != TAKE_MEASURED}),
                         "retakes": len({t["take_id"] for t in self.takes if t.get("attempt", 1) > 1 and not t.get("replay")})},
            "sets": [{**group, "takes": [{"take_id": take["take_id"], "record_id": take["record_id"],
                                          "selected": take["take_id"] in chosen} for take in group["takes"]]}
                     for group in self._sets.values()],
            "status": self.status, "finalized": self.finalized,
            "reason": self.reason, "detail": self.detail, "stopped_at": self.stopped_at,
            "not_measured": self.not_measured, "attempts": self.attempts, "wall_s": self.wall_s,
        }

    def joined(self) -> dict[str, Any]:
        """:meth:`to_dict` with each take read with the record it banked, as a
        round's readers join a kept take (ADR-0395)."""
        document = self.to_dict()
        return {**document, "sets": [{**group, "takes": [{**take, **self._banked.get(take["record_id"], {})}
                                                         for take in group["takes"]]} for group in document["sets"]]}
