# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared metadata, audio binding and role loading for banked captures.

Programs bind by content hash, never phase label. Pose identity uses declared
coordinates, never a seat index. Analysis remains outside this reader.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np

from jasper.audio_measurement.deconv import regularized_deconvolution_full
from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.audio_measurement.sweep import read_wav_mono
from jasper.json_fields import finite_float, sha256_file

from ..measurement_programs import POSE_KIND_BEARING, POSE_KIND_SEAT
from ..commissioning_evidence_store import EVIDENCE_ROOT
from ..run_manifest import RUN_MANIFEST_FILENAME
from .position_cycle import curves_for_take
from .record_index import measurement_documents, played_graph_fingerprint
from .round_inputs import (
    NO_ROUND_ARTIFACTS_REASON, RoundViewsError, round_artifact_dir, round_inputs,
)
from .take_impulses import IMPULSES_KEY, TakeImpulse, TakeImpulsesUnreadable, impulse_for, take_impulses

# --- refusals: every one names the input that was missing --------------------

REFUSE_NO_CAPTURES = "round_no_captures"
REFUSE_NO_PROGRAMS = "round_no_programs"
REFUSE_PROGRAM_UNMATCHED = "round_program_hash_unmatched"
REFUSE_RADIATED_BAND_MISSING = "round_radiated_band_missing"
REFUSE_CAPTURE_UNREADABLE = "round_capture_unreadable"
#: A branch role was asked of a take whose record kept no branch diagnostic;
#: only a take played in the ``branches`` regime keeps one (#5632 F8).
REFUSE_BRANCH_DIAGNOSTIC_MISSING = "round_branch_diagnostic_missing"
#: A role was asked of a take that kept impulses, none of them for that role.
REFUSE_ROLE_NOT_RECORDED = "round_role_not_recorded"


class RoundCapturesRefused(Exception):
    """A named refusal with the evidence behind it. Never a bare failure."""

    def __init__(self, reason: str, detail: Mapping[str, Any]) -> None:
        super().__init__(f"{reason}: {json.dumps(detail, sort_keys=True, default=str)}")
        self.reason = reason
        self.detail = dict(detail)


@dataclass
class PoseCapture:
    """One capture role, its raw response, and the shared source record."""

    capture_id: str
    phase: str | None
    wav: Path | None
    program: Path | None
    program_sha256: str
    azimuth_deg: float | None
    vertical_deg: float | None
    mark_distance_m: float | None
    radiated_band_hz: tuple[float, float]
    sample_rate: int
    ir: np.ndarray
    peak_idx: int
    pose_kind: str = POSE_KIND_BEARING
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
    def clocked(self) -> bool:
        """On the take's recording clock: a kept impulse or one its branch
        diagnostic retained. An impulse rebuilt from the whole program is not."""
        return "pre_guard_samples" in self.preprocessing

    @property
    def pose_key(self) -> str:
        """The FULL declared pose. Never a seat index (#3503)."""
        return _pose_key(
            self.azimuth_deg, self.vertical_deg, self.mark_distance_m,
            self.pose_kind, self.seat_offset_m, self.pose_driver,
        )


def doc_pose_key(doc: Mapping[str, Any]) -> str:
    """The pose a sidecar DOC declares, keyed as :attr:`PoseCapture.pose_key`.

    Readable before the capture is decoded, so a reader that filters poses on
    the doc can still name the ones it passed over (#3503).
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
    """The kind a doc declares and, for a seat, its ``(right, forward, up)``.

    A doc banked before poses had a kind is the bearing it always was.
    """
    kind = doc.get("pose_kind")
    kind = kind if isinstance(kind, str) and kind else POSE_KIND_BEARING
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
    kind: str = POSE_KIND_BEARING,
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


def _declared_program_sha(doc: Mapping[str, Any], root: Path) -> str | None:
    """Prefer the retained program hash; legacy records may bind by file bytes."""
    provenance = doc.get("provenance")
    stimulus = provenance.get("stimulus") if isinstance(provenance, Mapping) else None
    if not isinstance(stimulus, Mapping):
        return None
    declared = stimulus.get("wav_sha256")
    if isinstance(declared, str) and declared:
        return declared
    for key in ("wav_path", "path", "program_path"):
        named = stimulus.get(key)
        if isinstance(named, str) and named:
            candidate = Path(named)
            if not candidate.is_absolute():
                candidate = root / named
            if candidate.is_file():
                return sha256_file(candidate)
    return None


def radiated_band_of(
    doc: Mapping[str, Any], manifest: Mapping[str, Any] | None = None,
) -> tuple[float, float] | None:
    """The band this capture's DUT actually radiates, from its own curves.

    Public because :mod:`.feature_classifier` asks the same question of the
    sidecars it loads itself. Absent yields ``None`` rather than a default
    span: the un-intersected band priced a tweeter from 357 Hz where it has no
    output and over-reported by 3x (E5, #1969).
    """
    los: list[float] = []
    his: list[float] = []
    for curve in curves_for_take(doc, manifest):
        band = curve.get("band_hz") if isinstance(curve, Mapping) else None
        if isinstance(band, Sequence) and len(band) == 2:
            los.append(float(band[0]))
            his.append(float(band[1]))
    if not los:
        return None
    return (min(los), max(his))


def _capture_document(path: Path) -> Mapping[str, Any]:
    try:
        doc = json.loads(path.read_text())
        if not isinstance(doc, Mapping):
            raise ValueError("capture metadata is not an object")
        return doc
    except (OSError, ValueError) as exc:
        raise RoundCapturesRefused(
            REFUSE_CAPTURE_UNREADABLE, {"sidecar": path.name, "detail": str(exc)},
        ) from exc


#: A record, the WAV it names, its document (empty when unreadable) and the
#: fault that keeps it out of every view, if any.
_Record = tuple[Path, Path, Mapping[str, Any], RoundCapturesRefused | None]


class _Omission(NamedTuple):
    """A selected capture left out: what is published, its record, and why."""

    entry: dict[str, str]
    doc: Mapping[str, Any]
    fault: RoundCapturesRefused


def _capture_documents(round_dir: Path) -> tuple[Path, list[_Record]]:
    try:
        root = round_inputs(round_dir).session_dir
    except RoundViewsError as exc:
        if (round_dir / "bundle").exists():
            raise RoundCapturesRefused(
                REFUSE_CAPTURE_UNREADABLE, {"round_dir": str(round_dir), "detail": str(exc)},
            ) from exc
        root = round_dir
    artifact_dir, reason = round_artifact_dir(root)
    if artifact_dir is None and reason != NO_ROUND_ARTIFACTS_REASON:
        raise RoundCapturesRefused(
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
        records.append((path, wav, doc, None if problem is None else RoundCapturesRefused(
            REFUSE_CAPTURE_UNREADABLE, {"sidecar": path.name, "wav": str(wav), "detail": problem},
        )))
    for path in sorted(root.glob("summed/summed_*.json")):
        wav = path.with_suffix(".wav").resolve()
        if wav in claims:
            continue
        try:
            records.append((path, wav, _capture_document(path), None))
        except RoundCapturesRefused as exc:
            records.append((path, wav, {}, exc))
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
    """Read a round or bundle through canonical records, then legacy sidecars.

    ``select`` runs first, on the record alone, so a capture no reader asked
    for is never checked or decoded; an empty filtered result is valid. Each
    selected record binds its exact summed WAV and program. One that fails is
    never analyzed: it is appended to ``omitted`` as ``capture_id``,
    ``sidecar`` and ``reason``, and the rest answer. Raises
    :class:`RoundCapturesRefused` for missing or conflicting round input, and
    under the first failure's reason when every selected capture failed.
    """
    captures, skipped = _discover_captures(round_dir, select=select, roles=(role,))
    if omitted is not None:
        omitted += [omission.entry for omission in skipped]
    return captures


def _refused(fault: RoundCapturesRefused, skipped: list[_Omission]) -> RoundCapturesRefused:
    """``fault`` under its own reason, naming every capture left out beside it."""
    return RoundCapturesRefused(fault.reason, {**fault.detail, "omitted": [omission.entry for omission in skipped]})


def _discover_captures(
    round_dir: Path, *, select: Callable[[Mapping[str, Any]], bool] | None, roles: tuple[str, ...],
    clocked: bool = False,
) -> tuple[tuple[PoseCapture, ...], list[_Omission]]:
    round_dir, documents = _capture_documents(Path(round_dir))
    manifest: Mapping[str, Any] | None = None
    if not documents:
        raise RoundCapturesRefused(
            REFUSE_NO_CAPTURES,
            {"round_dir": str(round_dir), "looked_for": "**/summed/summed_*.json"},
        )
    programs: dict[str, Path] = {}
    for candidate in sorted(round_dir.glob("**/*program*.wav")):
        programs.setdefault(sha256_file(candidate), candidate)

    captures: list[PoseCapture] = []
    skipped: list[_Omission] = []
    program_audio: dict[str, tuple[np.ndarray, int]] = {}
    for sidecar, wav, doc, fault in documents:
        # A record with nothing readable in it cannot be deselected.
        if doc and select is not None and not select(doc):
            continue
        if fault is None:
            if not doc.get("curves") and manifest is None:
                artifact_dir, _ = round_artifact_dir(round_dir)
                manifest_path = artifact_dir / RUN_MANIFEST_FILENAME if artifact_dir else None
                manifest = _capture_document(manifest_path) if manifest_path and manifest_path.is_file() else {}
            try:
                captures += _bind_record(sidecar, wav, doc, roles, round_dir, programs, manifest, program_audio,
                                         clocked=clocked)
                continue
            except RoundCapturesRefused as exc:
                fault = exc
        skipped.append(_Omission({"capture_id": document_capture_id(doc) or sidecar.stem,
                                  "sidecar": sidecar.name, "reason": fault.reason}, doc, fault))
    if skipped and not captures:
        raise _refused(skipped[0].fault, skipped)
    return tuple(sorted(captures, key=lambda cap: cap.capture_id)), skipped


def _bind_record(
    sidecar: Path, wav: Path, doc: Mapping[str, Any], roles: tuple[str, ...], root: Path,
    programs: Mapping[str, Path], manifest: Mapping[str, Any] | None, program_audio: dict[str, tuple[np.ndarray, int]],
    *, clocked: bool,
) -> list[PoseCapture]:
    """One record's capture per role, or the refusal that keeps it out."""
    if not wav.is_file():
        raise RoundCapturesRefused(
            REFUSE_CAPTURE_UNREADABLE,
            {"sidecar": sidecar.name, "wav": str(wav), "detail": "capture WAV is missing"},
        )
    capture_sha = sha256_file(wav)
    if doc.get("wav_sha256") and capture_sha != doc["wav_sha256"]:
        raise RoundCapturesRefused(
            REFUSE_CAPTURE_UNREADABLE,
            {"sidecar": sidecar.name, "declared_capture_sha256": doc["wav_sha256"]},
        )
    sha = _declared_program_sha(doc, root)
    program = programs.get(sha) if sha is not None else None
    # A take that kept its impulses needs no program: nothing is deconvolved again.
    if program is None and not isinstance(doc.get(IMPULSES_KEY), Mapping):
        if not programs:
            raise RoundCapturesRefused(
                REFUSE_NO_PROGRAMS, {"round_dir": str(root), "looked_for": "**/*program*.wav"},
            )
        raise RoundCapturesRefused(
            REFUSE_PROGRAM_UNMATCHED,
            {
                "sidecar": sidecar.name,
                "declared_stimulus_sha256": sha,
                "programs_present": sorted(
                    {path.name for path in programs.values()}
                ),
                "note": (
                    "capture-to-program binding is by content hash; the "
                    "sidecar's declared stimulus phase is not consulted"
                ),
            },
        )
    band = radiated_band_of(doc, manifest)
    if band is None:
        raise RoundCapturesRefused(
            REFUSE_RADIATED_BAND_MISSING,
            {
                "sidecar": sidecar.name,
                "note": (
                    "a graded band is intersected with the band this "
                    "capture's DUT radiates; without it none is honest"
                ),
            },
        )
    try:
        kept = take_impulses(root, doc) if isinstance(doc.get(IMPULSES_KEY), Mapping) else None
    except TakeImpulsesUnreadable as exc:
        raise RoundCapturesRefused(REFUSE_CAPTURE_UNREADABLE, {"capture": str(wav), "detail": str(exc)}) from exc
    curves = {role: _role_curve(doc, manifest, role) for role in roles}
    responses = [_capture_response(doc, role, wav, program, program_audio, kept=kept, curve=curves[role],
                                   clocked=clocked) for role in roles]
    pose_kind, seat_offset_m = _doc_pose_category(doc)
    return [
        PoseCapture(
            capture_id=document_capture_id(doc) or sidecar.stem,
            phase=doc.get("phase") if isinstance(doc.get("phase"), str) else None,
            wav=wav,
            program=program,
            program_sha256=str(sha or ""),
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
            capture_sha256=capture_sha,
            preprocessing=preprocessing,
            record_path=sidecar,
            record_document=doc,
            curve=curves[role],
        )
        for role, (ir, rate, retained_band, preprocessing) in zip(roles, responses, strict=True)
    ]


def _role_curve(
    doc: Mapping[str, Any], manifest: Mapping[str, Any] | None, role: str,
) -> Mapping[str, Any]:
    """This take's banked curve for ``role``, or empty."""
    return next((curve for curve in curves_for_take(doc, manifest)
                 if isinstance(curve, Mapping) and curve.get("role") == role), {})


def _role_band(curve: Mapping[str, Any]) -> tuple[float, float] | None:
    band = curve.get("band_hz")
    return (float(band[0]), float(band[1])) if isinstance(band, Sequence) and len(band) == 2 else None


def _capture_response(
    doc: Mapping[str, Any], role: str, wav: Path, program: Path | None,
    program_audio: dict[str, tuple[np.ndarray, int]], *, kept: tuple[TakeImpulse, ...] | None,
    curve: Mapping[str, Any], clocked: bool,
) -> tuple[np.ndarray, int, tuple[float, float] | None, dict[str, Any]]:
    """One role's impulse: the one the take kept, else rebuilt from the recording.

    A per-driver take keeps no summed impulse; its summed read is the recording
    deconvolved against the whole program, off the take's recording clock.
    ``clocked`` refuses that read before it is made.
    """
    try:
        if kept is not None:
            stored = impulse_for(kept, role)
            if stored is not None:
                return stored.samples, stored.sample_rate_hz, _role_band(curve), {
                    "role": role, "impulse_source": "kept", "segment_id": stored.segment_id,
                    "pre_guard_samples": stored.origin_index,
                    "clock_shift_samples": stored.clock_shift_samples,
                    "microphone_correction": False,
                }
            if role != "summed":
                raise RoundCapturesRefused(REFUSE_ROLE_NOT_RECORDED, {
                    "role": role, "capture": str(wav),
                    "roles": sorted({one.role for one in kept}),
                })
        band = None
        diagnostic = doc.get("branch_diagnostic")
        retained = None
        preprocessing: dict[str, Any] = {}
        if isinstance(diagnostic, Mapping):
            retained = next((r for r in diagnostic["responses"] if r["role"] == role), None)
            if retained is None:
                raise RoundCapturesRefused(REFUSE_CAPTURE_UNREADABLE, {"role": role, "capture": str(wav)})
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
            rate = diagnostic["sample_rate_hz"]
            ir = np.asarray(retained["impulse"], dtype=np.float64)
            band = tuple(retained["band_hz"])
        else:
            if role != "summed" or clocked:
                raise RoundCapturesRefused(REFUSE_BRANCH_DIAGNOSTIC_MISSING, {"role": role, "capture": str(wav)})
            signal, rate = read_wav_mono(wav)
        if retained is None:
            if program is None:
                raise RoundCapturesRefused(REFUSE_PROGRAM_UNMATCHED, {"capture": str(wav)})
            program_key = str(program)
            if program_key not in program_audio:
                program_audio[program_key] = read_wav_mono(program)
            program_signal, program_rate = program_audio[program_key]
            if rate != program_rate:
                raise RoundCapturesRefused(
                    REFUSE_CAPTURE_UNREADABLE,
                    {
                        "capture": str(wav),
                        "detail": f"{rate} Hz capture against {program_rate} Hz program",
                    },
                )
            ir = regularized_deconvolution_full(signal, program_signal, rate).astype(
                np.float64
            )
        return ir, int(rate), band, preprocessing
    except (KeyError, TypeError, ValueError) as exc:
        raise RoundCapturesRefused(REFUSE_CAPTURE_UNREADABLE, {
            "capture": str(wav), "role": role, "detail": str(exc),
        }) from exc


# Published close-reference refusal names also serve the shared selector.
REFUSE_CLOSE_REFERENCE_UNREADABLE_ROUND = "close_reference_unreadable_round"
REFUSE_CLOSE_REFERENCE_NO_CAPTURE = "close_reference_no_capture"


def select_capture(
    round_dir: Path, *, capture_id: str | None = None, role: str = "summed",
    omitted: list[dict[str, str]] | None = None,
) -> PoseCapture:
    """The one capture a single-capture reader takes out of ``round_dir``.

    ``capture_id`` selects by the capture's own id or its WAV stem. With none,
    the on-axis capture wins: azimuth 0, elevation 0, first by capture id; if
    that take failed, only its repeat at the same pose under the same played
    graph stands in. Raises :class:`RoundCapturesRefused` rather than
    guessing. The choice is made on each sidecar DOC, so the poses the reader
    discards are never checked or deconvolved; a chosen one that fails lands
    in ``omitted`` as :func:`discover_captures` says.
    """
    return select_capture_roles(round_dir, capture_id=capture_id, roles=(role,), omitted=omitted)[role]


def select_capture_roles(
    round_dir: Path, *, capture_id: str | None, roles: tuple[str, ...],
    omitted: list[dict[str, str]] | None = None, clocked: bool = False,
) -> dict[str, PoseCapture]:
    """Read selected roles from one record and one set of verified audio bytes.

    ``clocked`` refuses a role the take would rebuild rather than kept, so
    every role read shares the take's recording clock.
    """
    root = Path(round_dir)
    if not root.is_dir():
        raise RoundCapturesRefused(REFUSE_CLOSE_REFERENCE_UNREADABLE_ROUND, {"round_dir": str(root)})
    seen: list[str] = []
    if capture_id is not None:
        def wanted(doc: Mapping[str, Any]) -> bool:
            declared = document_capture_id(doc)
            seen.append(str(declared) if declared else "")
            # A sidecar that declares no id takes its capture id from its own
            # file name, which this predicate cannot see; the WAV-stem match
            # below decides.
            return (
                not declared or str(declared) == capture_id
                or Path(str(doc.get("wav_path") or "")).stem == capture_id
            )

        found, skipped = _discover_captures(root, select=wanted, roles=roles, clocked=clocked)
        chosen = tuple(
            capture
            for capture in found
            if capture_id
            in (capture.capture_id, capture.wav.stem if capture.wav else None)
        )
        if len(chosen) != len(roles):
            raise RoundCapturesRefused(
                REFUSE_CLOSE_REFERENCE_NO_CAPTURE,
                {
                    "round_dir": str(root),
                    "capture_id": capture_id,
                    "captures": seen,
                    "matches": len(chosen) // len(roles),
                },
            )
    else:
        def on_axis_doc(doc: Mapping[str, Any]) -> bool:
            seen.append(doc_pose_key(doc))
            # A pose declared as anything but a number compares False here, the
            # same answer the decoded ``None`` gave.
            return doc.get("position_deg") == 0 and doc.get("vertical_deg") == 0

        found, skipped = _discover_captures(root, select=on_axis_doc, roles=roles, clocked=clocked)
        if not found:
            raise RoundCapturesRefused(
                REFUSE_CLOSE_REFERENCE_NO_CAPTURE,
                {
                    "round_dir": str(root),
                    "note": "no capture declares azimuth 0 / elevation 0",
                    "poses": seen,
                },
            )
        chosen = _on_axis_take(found, skipped, len(roles))
    if omitted is not None:
        omitted += [omission.entry for omission in skipped]
    return dict(zip(roles, chosen, strict=True))


def _on_axis_take(
    found: tuple[PoseCapture, ...], skipped: list[_Omission], n_roles: int,
) -> tuple[PoseCapture, ...]:
    """The first on-axis take by capture id, or its repeat when it was left out.

    Only a repeat at the same pose under the same played graph stands in: a
    behind pose shares the front's 0/0 bearing, so any other fallback reads a
    different take than the one asked for. A record nothing can read has no
    known pose, so nothing is provably its repeat.
    """
    takes = [found[index:index + n_roles] for index in range(0, len(found), n_roles)]
    unreadable = [omission for omission in skipped if not omission.doc]
    if unreadable:
        raise _refused(unreadable[0].fault, skipped)
    missed = min(skipped, key=lambda omission: omission.entry["capture_id"], default=None)
    if missed is None or takes[0][0].capture_id < missed.entry["capture_id"]:
        return takes[0]
    same = (doc_pose_key(missed.doc), played_graph_fingerprint(missed.doc))
    for take in takes:
        if (take[0].pose_key, take[0].graph_fingerprint) == same:
            return take
    raise _refused(missed.fault, skipped)


def capture_row(capture: PoseCapture) -> dict[str, Any]:
    """What a report says about a capture it read."""
    return {
        "capture_id": capture.capture_id,
        "phase": capture.phase,
        "pose_key": capture.pose_key,
        "wav": capture.wav.name if capture.wav else None,
        "program": capture.program.name if capture.program else None,
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
