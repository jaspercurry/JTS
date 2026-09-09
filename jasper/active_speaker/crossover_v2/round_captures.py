# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A banked round's summed captures, each bound to the program its BYTES were
played through and deconvolved into an impulse response.

Two verbs read a round this way — the gate sweep and the close reference — so
the binding lives here rather than once per verb. **The phase label is not the
program** (#3504): captures bind by content hash
(``provenance.stimulus.wav_sha256``), never by ``provenance.stimulus.phase``.
**The pose label is not the pose** (#3503): :attr:`PoseCapture.pose_key` is the
full declared (azimuth, elevation, distance) triple, never a seat index.
"""

from __future__ import annotations

from .record_index import played_graph_fingerprint

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from jasper.audio_measurement.bundles import sha256_file
from jasper.audio_measurement.deconv import regularized_deconvolution_full
from jasper.audio_measurement.sweep import read_wav_mono

from ..measurement_programs import POSE_KIND_BEARING, POSE_KIND_SEAT
from ..commissioning_evidence_store import EVIDENCE_ROOT
from .record_index import bundle_measurements
from .round_inputs import (
    NO_ROUND_ARTIFACTS_REASON, RoundViewsError, round_artifact_dir, round_inputs,
)

# --- refusals: every one names the input that was missing --------------------

REFUSE_NO_CAPTURES = "round_no_captures"
REFUSE_NO_PROGRAMS = "round_no_programs"
REFUSE_PROGRAM_UNMATCHED = "round_program_hash_unmatched"
REFUSE_RADIATED_BAND_MISSING = "round_radiated_band_missing"
REFUSE_CAPTURE_UNREADABLE = "round_capture_unreadable"


class RoundCapturesRefused(Exception):
    """A named refusal with the evidence behind it. Never a bare failure."""

    def __init__(self, reason: str, detail: Mapping[str, Any]) -> None:
        super().__init__(f"{reason}: {json.dumps(detail, sort_keys=True, default=str)}")
        self.reason = reason
        self.detail = dict(detail)


@dataclass
class PoseCapture:
    """One banked capture, its declared pose, and its impulse response.

    Data only: whatever a reader derives from ``ir`` is held beside this rather
    than written back onto it. The gate-sweep engine computes from five fields
    only — ``capture_id``, ``radiated_band_hz``, ``sample_rate``, ``ir``,
    ``peak_idx``; the rest are disclosure, and a caller holding captures in
    memory rather than on disk fills the two paths with ``None``.
    """

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
    candidate_id: str = ""
    graph_fingerprint: str = ""
    capture_sha256: str = ""
    preprocessing: Mapping[str, Any] = field(default_factory=dict)
    record_path: Path | None = None

    @property
    def pose_key(self) -> str:
        """The FULL declared pose. Never a seat index (#3503)."""
        return _pose_key(
            self.azimuth_deg, self.vertical_deg, self.mark_distance_m,
            self.pose_kind, self.seat_offset_m,
        )


def doc_pose_key(doc: Mapping[str, Any]) -> str:
    """The pose a sidecar DOC declares, keyed as :attr:`PoseCapture.pose_key`.

    Readable before the capture is decoded, so a reader that filters poses on
    the doc can still name the ones it passed over (#3503).
    """
    return _pose_key(
        _number(doc.get("position_deg")),
        _number(doc.get("vertical_deg")),
        _number(doc.get("mark_distance_m")),
        *_doc_pose_category(doc),
    )


def _doc_pose_category(doc: Mapping[str, Any]) -> tuple[str, tuple[float, ...] | None]:
    """The kind a doc declares and, for a seat, its ``(right, forward, up)``.

    A doc banked before poses had a kind is the bearing it always was.
    """
    kind = doc.get("pose_kind")
    kind = kind if isinstance(kind, str) and kind else POSE_KIND_BEARING
    offset = doc.get("seat_offset_m")
    if kind != POSE_KIND_SEAT or not isinstance(offset, Sequence):
        return kind, None
    numbers = [_number(v) for v in offset]
    if len(numbers) != 3 or any(v is None for v in numbers):
        return kind, None
    return kind, tuple(numbers)  # type: ignore[arg-type]


def _pose_key(
    azimuth_deg: float | None,
    vertical_deg: float | None,
    mark_distance_m: float | None,
    kind: str = POSE_KIND_BEARING,
    seat_offset_m: tuple[float, ...] | None = None,
) -> str:
    key = "az{}_el{}_d{}".format(
        _pose_field(azimuth_deg), _pose_field(vertical_deg), _pose_field(mark_distance_m)
    )
    # A bearing keys exactly as it did before poses had a kind (#3503).
    if kind != POSE_KIND_BEARING:
        key = f"{kind}_{key}"
    if seat_offset_m is not None:
        key += "_r{}_f{}_u{}".format(*(_pose_field(v) for v in seat_offset_m))
    return key


def _pose_field(value: float | None) -> str:
    return "na" if value is None else f"{value:+.2f}"


def _declared_program_sha(doc: Mapping[str, Any], root: Path) -> str | None:
    """The program hash this sidecar declares, or one hashed from its bytes.

    ``provenance.stimulus.wav_sha256`` is the authority; absent it the stimulus
    PATH is hashed from its own bytes — still content, never
    ``provenance.stimulus.phase``, which declares ``verify`` on captures whose
    played bytes were ``cloud_verify`` (#3504).
    """
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


def radiated_band_of(doc: Mapping[str, Any]) -> tuple[float, float] | None:
    """The band this capture's DUT actually radiates, from its own curves.

    Public because :mod:`.feature_classifier` asks the same question of the
    sidecars it loads itself. Absent yields ``None`` rather than a default
    span: the un-intersected band priced a tweeter from 357 Hz where it has no
    output and over-reported by 3x (E5, #1969).
    """
    curves = doc.get("curves")
    if not isinstance(curves, Sequence):
        return None
    los: list[float] = []
    his: list[float] = []
    for curve in curves:
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


def _capture_documents(round_dir: Path) -> tuple[Path, list[tuple[Path, Path, Mapping[str, Any]]]]:
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
    documents: dict[Path, tuple[Path, Mapping[str, Any]]] = {}
    for row in bundle_measurements(root):
        path = root / EVIDENCE_ROOT / "artifacts" / row.path
        doc = _capture_document(path)
        named = doc.get("wav_path")
        if not isinstance(named, str) or not named:
            continue
        wav = (root / named).resolve()
        if wav.parent != (root / "summed").resolve():
            continue
        if not doc.get("wav_sha256") or wav in documents:
            raise RoundCapturesRefused(
                REFUSE_CAPTURE_UNREADABLE,
                {"sidecar": path.name, "wav": str(wav), "detail": (
                    "multiple canonical records name this WAV" if wav in documents
                    else "canonical capture hash is missing"
                )},
            )
        documents[wav] = (path, doc)
    for path in sorted(root.glob("summed/summed_*.json")):
        wav = path.with_suffix(".wav").resolve()
        if wav not in documents:
            documents[wav] = (path, _capture_document(path))
    return root, [(path, wav, doc) for wav, (path, doc) in documents.items()]


def discover_captures(
    round_dir: Path,
    *,
    select: Callable[[Mapping[str, Any]], bool] | None = None,
    role: str = "summed",
) -> tuple[PoseCapture, ...]:
    """Read a round or bundle through canonical records, then legacy sidecars.

    Each record binds its exact summed WAV and program. ``select`` runs before
    decoding, after those bindings are checked; an empty filtered result is
    valid. Raises :class:`RoundCapturesRefused` for missing or conflicting input.
    """
    round_dir, documents = _capture_documents(Path(round_dir))
    if not documents:
        raise RoundCapturesRefused(
            REFUSE_NO_CAPTURES,
            {"round_dir": str(round_dir), "looked_for": "**/summed/summed_*.json"},
        )
    programs: dict[str, Path] = {}
    for candidate in sorted(round_dir.glob("**/*program*.wav")):
        programs.setdefault(sha256_file(candidate), candidate)
    if not programs:
        raise RoundCapturesRefused(
            REFUSE_NO_PROGRAMS,
            {"round_dir": str(round_dir), "looked_for": "**/*program*.wav"},
        )

    captures: list[PoseCapture] = []
    # Decoded once per unique program, not once per capture.
    program_audio: dict[str, tuple[np.ndarray, int]] = {}
    for sidecar, wav, doc in documents:
        if not wav.is_file():
            raise RoundCapturesRefused(
                REFUSE_CAPTURE_UNREADABLE,
                {"sidecar": sidecar.name, "wav": str(wav), "detail": "capture WAV is missing"},
            )
        capture_sha = doc.get("wav_sha256")
        if capture_sha and sha256_file(wav) != capture_sha:
            raise RoundCapturesRefused(
                REFUSE_CAPTURE_UNREADABLE,
                {"sidecar": sidecar.name, "declared_capture_sha256": capture_sha},
            )
        sha = _declared_program_sha(doc, round_dir)
        program = programs.get(sha) if sha is not None else None
        if program is None:
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
        if select is not None and not select(doc):
            continue
        band = radiated_band_of(doc)
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
                "microphone_correction": False,
            }
            rate = diagnostic["sample_rate_hz"]
            ir = np.asarray(retained["impulse"], dtype=np.float64)
            band = tuple(retained["band_hz"])
        else:
            if role != "summed":
                raise RoundCapturesRefused(REFUSE_CAPTURE_UNREADABLE, {"role": role, "capture": str(wav), "detail": "take has no retained branch diagnostic"})
            signal, rate = read_wav_mono(wav)
        if retained is None:
            program_key = str(sha)
            if program_key not in program_audio:
                program_audio[program_key] = read_wav_mono(program)
            program_signal, program_rate = program_audio[program_key]
            if rate != program_rate:
                raise RoundCapturesRefused(
                    REFUSE_CAPTURE_UNREADABLE,
                    {
                        "sidecar": sidecar.name,
                        "detail": f"{rate} Hz capture against {program_rate} Hz program",
                    },
                )
            ir = regularized_deconvolution_full(signal, program_signal, rate).astype(
                np.float64
            )
        pose_kind, seat_offset_m = _doc_pose_category(doc)
        captures.append(
            PoseCapture(
                capture_id=str(doc.get("take_id") or doc.get("position_id") or sidecar.stem),
                phase=doc.get("phase") if isinstance(doc.get("phase"), str) else None,
                wav=wav,
                program=program,
                program_sha256=str(sha),
                azimuth_deg=_number(doc.get("position_deg")),
                vertical_deg=_number(doc.get("vertical_deg")),
                mark_distance_m=_number(doc.get("mark_distance_m")),
                radiated_band_hz=band,
                sample_rate=int(rate),
                ir=ir,
                peak_idx=int(np.argmax(np.abs(ir))),
                pose_kind=pose_kind,
                seat_offset_m=seat_offset_m,
                candidate_id=str(doc.get("candidate_id") or ""),
                graph_fingerprint=played_graph_fingerprint(doc),
                capture_sha256=sha256_file(wav),
                preprocessing=preprocessing,
                record_path=sidecar,
            )
        )
    return tuple(sorted(captures, key=lambda cap: cap.capture_id))


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


# --------------------------------------------------------------------------- #
# which capture of a round a single-capture reader takes
# --------------------------------------------------------------------------- #

#: :func:`select_capture`'s own two. The STRINGS keep the close reference's
#: name because they are published: they are the ``reason`` its report and its
#: CLI's exit code are keyed on.
REFUSE_CLOSE_REFERENCE_UNREADABLE_ROUND = "close_reference_unreadable_round"
REFUSE_CLOSE_REFERENCE_NO_CAPTURE = "close_reference_no_capture"


def select_capture(
    round_dir: Path, *, capture_id: str | None = None, role: str = "summed"
) -> PoseCapture:
    """The one capture a single-capture reader takes out of ``round_dir``.

    ``capture_id`` selects by the capture's own id or its WAV stem. With none,
    the on-axis capture wins: azimuth 0, elevation 0, first by capture id.
    Raises :class:`RoundCapturesRefused` rather than guessing. The choice is
    made on each sidecar DOC, so the poses the reader discards are never
    deconvolved.
    """
    root = Path(round_dir)
    if not root.is_dir():
        raise RoundCapturesRefused(REFUSE_CLOSE_REFERENCE_UNREADABLE_ROUND, {"round_dir": str(root)})
    seen: list[str] = []
    if capture_id is not None:
        def wanted(doc: Mapping[str, Any]) -> bool:
            declared = doc.get("take_id") or doc.get("position_id")
            seen.append(str(declared) if declared else "")
            # A sidecar that declares no id takes its capture id from its own
            # file name, which this predicate cannot see; the WAV-stem match
            # below decides.
            return (
                not declared or str(declared) == capture_id
                or Path(str(doc.get("wav_path") or "")).stem == capture_id
            )

        named = [
            capture
            for capture in discover_captures(root, select=wanted, role=role)
            if capture_id
            in (capture.capture_id, capture.wav.stem if capture.wav else None)
        ]
        if len(named) != 1:
            raise RoundCapturesRefused(
                REFUSE_CLOSE_REFERENCE_NO_CAPTURE,
                {
                    "round_dir": str(root),
                    "capture_id": capture_id,
                    "captures": seen,
                    "matches": len(named),
                },
            )
        return named[0]

    def on_axis_doc(doc: Mapping[str, Any]) -> bool:
        seen.append(doc_pose_key(doc))
        # A pose declared as anything but a number compares False here, the
        # same answer the decoded ``None`` gave.
        return doc.get("position_deg") == 0 and doc.get("vertical_deg") == 0

    on_axis = discover_captures(root, select=on_axis_doc, role=role)
    if not on_axis:
        raise RoundCapturesRefused(
            REFUSE_CLOSE_REFERENCE_NO_CAPTURE,
            {
                "round_dir": str(root),
                "note": "no capture declares azimuth 0 / elevation 0",
                "poses": seen,
            },
        )
    return on_axis[0]


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
