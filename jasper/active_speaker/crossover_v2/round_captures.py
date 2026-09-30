# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared metadata and role loading for banked captures.

A take reads from its record and the impulses it kept; no recording or
program is opened (ADR-0397). Pose identity uses declared coordinates, never
a seat index. Analysis remains outside this reader.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np

from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.audio_measurement.evidence_reasons import TAKE_CURVES_NOT_BANKED, EvidenceUnavailable
from jasper.platform.json_fields import finite_float

from ..measurement_programs import POSE_KIND_BEARING, POSE_KIND_SEAT
from ..commissioning_evidence_store import EVIDENCE_ROOT
from .contracts import BANKED_TAKE_GLOB
from .position_cycle import take_curve, take_window
from .record_index import measurement_documents, played_graph_fingerprint, take_pose_kind
from .round_inputs import (
    NO_ROUND_ARTIFACTS_REASON, RoundViewsError, round_artifact_dir, round_inputs,
)
from .take_impulses import IMPULSES_KEY, TakeImpulse, TakeImpulsesUnreadable, impulse_for, take_impulses

# --- refusals: every one names the input that was missing --------------------

REFUSE_NO_CAPTURES = "round_no_captures"
REFUSE_RADIATED_BAND_MISSING = "round_radiated_band_missing"
REFUSE_CAPTURE_UNREADABLE = "round_capture_unreadable"
#: A role was asked of a take whose kept impulses, and branch diagnostic if it
#: has one (#5632), hold none for that role: a MEASURE take keeps no summed one.
REFUSE_ROLE_NOT_RECORDED = "round_role_not_recorded"


@dataclass
class PoseCapture:
    """One capture role, its raw response, and the shared source record."""

    capture_id: str
    phase: str | None
    wav: Path | None
    program_sha256: str
    azimuth_deg: float | None
    vertical_deg: float | None
    mark_distance_m: float | None
    radiated_band_hz: tuple[float, float]
    sample_rate: int
    ir: np.ndarray
    peak_idx: int
    pose_kind: str
    seat_offset_m: tuple[float, ...] | None = None
    pose_driver: str = ""
    candidate_id: str = ""
    graph_fingerprint: str = ""
    capture_sha256: str = ""
    preprocessing: Mapping[str, Any] = field(default_factory=dict)
    record_path: Path | None = None
    record_document: Mapping[str, Any] = field(default_factory=dict, repr=False)
    #: This role's banked curve (its gate window, floors and band); empty on a
    #: record banked without one.
    curve: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def pose_key(self) -> str:
        """The FULL declared pose. Never a seat index (#3503)."""
        return _pose_key(
            self.azimuth_deg, self.vertical_deg, self.mark_distance_m,
            self.pose_kind, self.seat_offset_m, self.pose_driver,
        )


def doc_pose_key(doc: Mapping[str, Any]) -> str:
    """The pose a take record declares, keyed as :attr:`PoseCapture.pose_key`.

    Readable before the capture is read, so a reader that filters poses on
    the record can still name the ones it passed over (#3503).
    """
    return _pose_key(
        finite_float(doc.get("position_deg")),
        finite_float(doc.get("vertical_deg")),
        finite_float(doc.get("mark_distance_m")),
        *_doc_pose_category(doc),
        _doc_pose_driver(doc),
    )


def _doc_pose_driver(doc: Mapping[str, Any]) -> str:
    driver = doc.get("pose_driver")
    return driver if isinstance(driver, str) else ""


def _doc_pose_category(doc: Mapping[str, Any]) -> tuple[str, tuple[float, ...] | None]:
    """The kind a doc declares and, for a seat, its ``(right, forward, up)``."""
    kind = take_pose_kind(doc)
    offset = doc.get("seat_offset_m")
    if kind != POSE_KIND_SEAT or not isinstance(offset, Sequence):
        return kind, None
    numbers = [finite_float(v) for v in offset]
    if len(numbers) != 3 or any(v is None for v in numbers):
        return kind, None
    return kind, tuple(numbers)  # type: ignore[arg-type]


def _pose_key(
    azimuth_deg: float | None,
    vertical_deg: float | None,
    mark_distance_m: float | None,
    kind: str,
    seat_offset_m: tuple[float, ...] | None = None,
    driver: str = "",
) -> str:
    key = "az{}_el{}_d{}".format(
        _pose_field(azimuth_deg), _pose_field(vertical_deg), _pose_field(mark_distance_m)
    )
    # A bearing keys exactly as it did before poses had a kind (#3503).
    if kind != POSE_KIND_BEARING:
        key = f"{kind}_{key}"
    if seat_offset_m is not None:
        key += "_r{}_f{}_u{}".format(*(_pose_field(v) for v in seat_offset_m))
    # The base key rounds distance to the centimetre; a driver's pose keys its millimetres (ADR-0360).
    if driver:
        key += f"_{driver}_{'na' if mark_distance_m is None else f'{mark_distance_m * 1000:g}'}mm"
    return key


def _pose_field(value: float | None) -> str:
    return "na" if value is None else f"{value:+.2f}"


def _declared_program_sha(doc: Mapping[str, Any]) -> str | None:
    """The program hash the take's provenance recorded, or ``None``."""
    provenance = doc.get("provenance")
    stimulus = provenance.get("stimulus") if isinstance(provenance, Mapping) else None
    if not isinstance(stimulus, Mapping):
        return None
    declared = stimulus.get("wav_sha256")
    if isinstance(declared, str) and declared:
        return declared
    return None


def radiated_band_of(doc: Mapping[str, Any]) -> tuple[float, float] | None:
    """The band this capture's DUT actually radiates, from its own curves.

    Public because :mod:`.feature_classifier` asks the same question of the
    records it loads itself. Absent yields ``None`` rather than a default
    span: the un-intersected band priced a tweeter from 357 Hz where it has no
    output and over-reported by 3x (#1969).
    """
    los: list[float] = []
    his: list[float] = []
    for curve in doc.get("curves") or ():
        band = curve.get("band_hz") if isinstance(curve, Mapping) else None
        if isinstance(band, Sequence) and len(band) == 2:
            los.append(float(band[0]))
            his.append(float(band[1]))
    if not los:
        return None
    return (min(los), max(his))


#: A record, the WAV it names, its document and the fault that keeps it out of
#: every view, if any.
_Record = tuple[Path, Path, Mapping[str, Any], EvidenceUnavailable | None]


class _Omission(NamedTuple):
    """A selected capture left out: what is published, and why."""

    entry: dict[str, str]
    fault: EvidenceUnavailable


def _capture_documents(round_dir: Path) -> tuple[Path, list[_Record]]:
    try:
        root = round_inputs(round_dir).session_dir
    except RoundViewsError as exc:
        if (round_dir / "bundle").exists():
            raise EvidenceUnavailable(
                REFUSE_CAPTURE_UNREADABLE, {"round_dir": str(round_dir), "detail": str(exc)},
            ) from exc
        root = round_dir
    artifact_dir, reason = round_artifact_dir(root)
    if artifact_dir is None and reason != NO_ROUND_ARTIFACTS_REASON:
        raise EvidenceUnavailable(
            REFUSE_CAPTURE_UNREADABLE, {"round_dir": str(round_dir), "detail": reason},
        )
    canonical: list[tuple[Path, Path, Mapping[str, Any]]] = []
    for row, doc in measurement_documents(root):
        named = doc.get("wav_path")
        if not isinstance(named, str) or not named:
            continue
        wav = (root / named).resolve()
        if wav.parent == (root / "summed").resolve():
            canonical.append((root / EVIDENCE_ROOT / "artifacts" / row.path, wav, doc))
    claims = Counter(wav for _, wav, _ in canonical)
    records: list[_Record] = []
    for path, wav, doc in canonical:
        problem = (
            "multiple canonical records name this WAV" if claims[wav] > 1
            else None if doc.get("wav_sha256") else "canonical capture hash is missing"
        )
        records.append((path, wav, doc, None if problem is None else EvidenceUnavailable(
            REFUSE_CAPTURE_UNREADABLE, {"sidecar": path.name, "wav": str(wav), "detail": problem},
        )))
    return root, records


def document_capture_id(doc: Mapping[str, Any]) -> str | None:
    value = doc.get("take_id") or doc.get("position_id")
    return str(value) if value else None


def discover_captures(
    round_dir: Path,
    *,
    select: Callable[[Mapping[str, Any]], bool] | None = None,
    role: str = "summed",
    omitted: list[dict[str, str]] | None = None,
) -> tuple[PoseCapture, ...]:
    """Read a round or bundle through its take records.

    ``select`` runs first, on the record alone, so a capture no reader asked
    for is never checked or read; an empty filtered result is valid. Each
    selected record reads the impulse it kept for ``role``. One that fails is
    never analyzed: it is appended to ``omitted`` as ``capture_id``,
    ``sidecar`` and ``reason``, and the rest answer. Raises
    :class:`EvidenceUnavailable` for missing or conflicting round input, and
    under the first failure's reason when every selected capture failed.
    """
    captures, skipped = _discover_captures(round_dir, select=select, roles=(role,))
    if omitted is not None:
        omitted += [omission.entry for omission in skipped]
    return captures


def _refused(fault: EvidenceUnavailable, skipped: list[_Omission]) -> EvidenceUnavailable:
    """``fault`` under its own reason, naming every capture left out beside it."""
    return EvidenceUnavailable(fault.reason, {**fault.detail, "omitted": [omission.entry for omission in skipped]})


def _discover_captures(
    round_dir: Path, *, select: Callable[[Mapping[str, Any]], bool] | None, roles: tuple[str, ...],
) -> tuple[tuple[PoseCapture, ...], list[_Omission]]:
    round_dir, documents = _capture_documents(Path(round_dir))
    if not documents:
        raise EvidenceUnavailable(
            REFUSE_NO_CAPTURES,
            {"round_dir": str(round_dir), "looked_for": f"{EVIDENCE_ROOT}/artifacts/{BANKED_TAKE_GLOB}"},
        )
    captures: list[PoseCapture] = []
    skipped: list[_Omission] = []
    for sidecar, wav, doc, fault in documents:
        if select is not None and not select(doc):
            continue
        if fault is None:
            try:
                captures += record_captures(doc, roles, round_dir, record_path=sidecar, wav=wav,
                                            program_sha256=str(_declared_program_sha(doc) or ""),
                                            capture_sha256=str(doc["wav_sha256"]))
                continue
            except EvidenceUnavailable as exc:
                fault = exc
        skipped.append(_Omission({"capture_id": document_capture_id(doc) or sidecar.stem,
                                  "sidecar": sidecar.name, "reason": fault.reason}, fault))
    if skipped and not captures:
        raise _refused(skipped[0].fault, skipped)
    return tuple(sorted(captures, key=lambda cap: cap.capture_id)), skipped


def record_captures(
    doc: Mapping[str, Any], roles: tuple[str, ...], root: Path, *,
    record_path: Path, wav: Path, program_sha256: str = "", capture_sha256: str = "",
) -> list[PoseCapture]:
    """One record's capture per role, from its banked fields; ``wav`` only names
    the recording, which is never opened."""
    band = radiated_band_of(doc)
    if band is None:
        raise EvidenceUnavailable(
            REFUSE_RADIATED_BAND_MISSING,
            {
                "sidecar": record_path.name,
                "note": (
                    "a graded band is intersected with the band this "
                    "capture's DUT radiates; without it none is honest"
                ),
            },
        )
    try:
        kept = take_impulses(root, doc) if isinstance(doc.get(IMPULSES_KEY), Mapping) else None
    except TakeImpulsesUnreadable as exc:
        raise EvidenceUnavailable(REFUSE_CAPTURE_UNREADABLE, {"capture": str(wav), "detail": str(exc)}) from exc
    curves = {role: take_curve(doc, role, take_window(doc)) or {} for role in roles}
    responses = [_capture_response(doc, role, wav, kept=kept, curve=curves[role]) for role in roles]
    pose_kind, seat_offset_m = _doc_pose_category(doc)
    return [
        PoseCapture(
            capture_id=document_capture_id(doc) or record_path.stem,
            phase=doc.get("phase") if isinstance(doc.get("phase"), str) else None,
            wav=wav,
            program_sha256=program_sha256,
            azimuth_deg=finite_float(doc.get("position_deg")),
            vertical_deg=finite_float(doc.get("vertical_deg")),
            mark_distance_m=finite_float(doc.get("mark_distance_m")),
            radiated_band_hz=retained_band or band,
            sample_rate=int(rate),
            ir=ir,
            peak_idx=int(np.argmax(np.abs(ir))),
            pose_kind=pose_kind,
            seat_offset_m=seat_offset_m,
            pose_driver=_doc_pose_driver(doc),
            candidate_id=str(doc.get("candidate_id") or ""),
            graph_fingerprint=played_graph_fingerprint(doc),
            capture_sha256=capture_sha256,
            preprocessing=preprocessing,
            record_path=record_path,
            record_document=doc,
            curve=curves[role],
        )
        for role, (ir, rate, retained_band, preprocessing) in zip(roles, responses, strict=True)
    ]


def _role_band(curve: Mapping[str, Any]) -> tuple[float, float] | None:
    band = curve.get("band_hz")
    return (float(band[0]), float(band[1])) if isinstance(band, Sequence) and len(band) == 2 else None


def _capture_response(
    doc: Mapping[str, Any], role: str, wav: Path, *, kept: tuple[TakeImpulse, ...] | None,
    curve: Mapping[str, Any],
) -> tuple[np.ndarray, int, tuple[float, float] | None, dict[str, Any]]:
    """One role's impulse: the one the take kept, else the one its branch
    diagnostic retained, both on the take's recording clock. A take that kept
    neither refuses by that field; nothing is rebuilt from its recording."""
    try:
        stored = None if kept is None else impulse_for(kept, role)
        if stored is not None:
            return stored.samples, stored.sample_rate_hz, _role_band(curve), {
                "role": role, "impulse_source": "kept", "segment_id": stored.segment_id,
                "pre_guard_samples": stored.origin_index,
                "clock_shift_samples": stored.clock_shift_samples,
                "microphone_correction": False,
            }
        found = doc.get("branch_diagnostic")
        diagnostic: Mapping[str, Any] = found if isinstance(found, Mapping) else {}
        responses = diagnostic["responses"] if diagnostic else None
        if kept is None and responses is None:
            raise EvidenceUnavailable(TAKE_CURVES_NOT_BANKED, {"capture": str(wav), "field": IMPULSES_KEY, "role": role})
        retained = next((r for r in responses or () if r["role"] == role), None)
        if retained is None:
            raise EvidenceUnavailable(REFUSE_ROLE_NOT_RECORDED, {
                "role": role, "capture": str(wav),
                "roles": sorted({one.role for one in kept or ()} | {r["role"] for r in responses or ()}),
            })
        preprocessing = {
            "role": role, "timing_reference": diagnostic["timing_reference"],
            "clock_epsilon_ppm": diagnostic["clock_epsilon_ppm"],
            "clock_shift_samples": retained["clock_shift_samples"],
            "segment_id": retained["segment_id"],
            "pre_guard_samples": retained["pre_guard_samples"],
            "scheduled_start_sample": retained["scheduled_start_sample"],
            "global_offset_samples": diagnostic["global_offset_samples"],
            "microphone_correction": False, "impulse_source": "branch_diagnostic",
        }
        ir = np.asarray(retained["impulse"], dtype=np.float64)
        return ir, int(diagnostic["sample_rate_hz"]), tuple(retained["band_hz"]), preprocessing
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceUnavailable(REFUSE_CAPTURE_UNREADABLE, {
            "capture": str(wav), "role": role, "detail": str(exc),
        }) from exc


# Published names the shared selector raises; the close-reference view that
# named them retired (ADR-0366 §5).
REFUSE_CLOSE_REFERENCE_UNREADABLE_ROUND = "close_reference_unreadable_round"
REFUSE_CLOSE_REFERENCE_NO_CAPTURE = "close_reference_no_capture"


def select_capture(
    round_dir: Path, *, capture_id: str, role: str = "summed",
    omitted: list[dict[str, str]] | None = None,
) -> PoseCapture:
    """The one capture a single-capture reader takes out of ``round_dir``.

    ``capture_id`` selects by the capture's own id or its WAV stem. Raises
    :class:`EvidenceUnavailable` rather than guessing. The choice is made on
    each record, so the takes the reader discards are never checked or read;
    a chosen one that fails lands in ``omitted`` as :func:`discover_captures`
    says.
    """
    return select_capture_roles(round_dir, capture_id=capture_id, roles=(role,), omitted=omitted)[role]


def select_capture_roles(
    round_dir: Path, *, capture_id: str, roles: tuple[str, ...],
    omitted: list[dict[str, str]] | None = None,
) -> dict[str, PoseCapture]:
    """Read selected roles from one record, every role on the take's recording clock."""
    root = Path(round_dir)
    if not root.is_dir():
        raise EvidenceUnavailable(REFUSE_CLOSE_REFERENCE_UNREADABLE_ROUND, {"round_dir": str(root)})
    seen: list[str] = []

    def wanted(doc: Mapping[str, Any]) -> bool:
        declared = document_capture_id(doc)
        seen.append(str(declared) if declared else "")
        # A record that declares no id takes its capture id from its own file
        # name, which this predicate cannot see; the WAV-stem match below decides.
        return (
            not declared or str(declared) == capture_id
            or Path(str(doc.get("wav_path") or "")).stem == capture_id
        )

    found, skipped = _discover_captures(root, select=wanted, roles=roles)
    chosen = tuple(
        capture
        for capture in found
        if capture_id
        in (capture.capture_id, capture.wav.stem if capture.wav else None)
    )
    if len(chosen) != len(roles):
        raise EvidenceUnavailable(
            REFUSE_CLOSE_REFERENCE_NO_CAPTURE,
            {
                "round_dir": str(root),
                "capture_id": capture_id,
                "captures": seen,
                "matches": len(chosen) // len(roles),
            },
        )
    if omitted is not None:
        omitted += [omission.entry for omission in skipped]
    return dict(zip(roles, chosen, strict=True))


def capture_row(capture: PoseCapture) -> dict[str, Any]:
    """What a report says about a capture it read."""
    return {
        "capture_id": capture.capture_id,
        "phase": capture.phase,
        "pose_key": capture.pose_key,
        "wav": capture.wav.name if capture.wav else None,
        "position_deg": capture.azimuth_deg,
        "vertical_deg": capture.vertical_deg,
        "mark_distance_m": capture.mark_distance_m,
        "stimulus_wav_sha256": capture.program_sha256,
        "capture_wav_sha256": capture.capture_sha256,
        "candidate_id": capture.candidate_id,
        "preprocessing": dict(capture.preprocessing),
        "graph_fingerprint": capture.graph_fingerprint,
    }


def capture_fingerprint(capture: PoseCapture) -> str:
    """Bind audio, retained analysis and exact context, independent of storage paths."""
    document = capture.record_document
    provenance = document.get("provenance") or {}
    return json_fingerprint({
        "capture_id": capture.capture_id,
        "capture_wav_sha256": capture.capture_sha256,
        "stimulus_wav_sha256": capture.program_sha256,
        "candidate_id": capture.candidate_id,
        "graph_fingerprint": capture.graph_fingerprint,
        "sample_rate_hz": capture.sample_rate,
        "pose": [capture.azimuth_deg, capture.vertical_deg, capture.mark_distance_m,
                 capture.pose_kind, list(capture.seat_offset_m) if capture.seat_offset_m else None],
        "branch_diagnostic": document.get("branch_diagnostic"),
        "volume_db": {key: provenance.get(key) for key in ("main_volume_db", "session_volume_db")},
    })
