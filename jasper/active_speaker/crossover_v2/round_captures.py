# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A banked round's summed captures, each bound to the program its BYTES were
played through and deconvolved into an impulse response.

Two verbs read a round this way — the gate sweep (:mod:`.gate_sweep`) and the
close reference (:mod:`.close_reference`) — so the binding lives here rather
than once per verb, where the two could drift.

**The phase label is not the program** (#3504). Captures bind by content hash
(``provenance.stimulus.wav_sha256``), never by ``provenance.stimulus.phase``,
which declares ``verify`` on five of six captures of the round these
instruments were built from. **The pose label is not the pose** (#3503):
:attr:`PoseCapture.pose_key` is the full declared (azimuth, elevation,
distance) triple, never a seat index at an assumed common height.

Reads only: nothing here plays, writes or decides.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from jasper.audio_measurement.deconv import regularized_deconvolution_full
from jasper.audio_measurement.sweep import read_wav_mono

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

    The curve dicts are scratch for whoever gates it — filled by the reader
    that computes them, empty for a reader that does not.
    """

    capture_id: str
    phase: str | None
    wav: Path
    program: Path
    program_sha256: str
    azimuth_deg: float | None
    vertical_deg: float | None
    mark_distance_m: float | None
    radiated_band_hz: tuple[float, float]
    sample_rate: int
    ir: np.ndarray
    peak_idx: int
    reference_const_db: float = 0.0
    #: Normalised dB on the analysis grid, keyed by rung in ms.
    curves: dict[float, np.ndarray] = field(default_factory=dict)
    #: The same curves with their one-octave broad tilt removed.
    detrended: dict[float, np.ndarray] = field(default_factory=dict)

    @property
    def pose_key(self) -> str:
        """The FULL declared pose. Never a seat index (#3503)."""
        return "az{}_el{}_d{}".format(
            _pose_field(self.azimuth_deg),
            _pose_field(self.vertical_deg),
            _pose_field(self.mark_distance_m),
        )


def _pose_field(value: float | None) -> str:
    return "na" if value is None else f"{value:+.2f}"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _declared_program_sha(doc: Mapping[str, Any], root: Path) -> str | None:
    """The program hash this sidecar declares, or one hashed from its bytes.

    The sidecar's ``provenance.stimulus.wav_sha256`` is the authority. When
    it is absent the stimulus PATH is hashed from its own bytes instead —
    still content, never ``provenance.stimulus.phase``, which declares
    ``verify`` on captures whose played bytes were ``cloud_verify`` (#3504).
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
                return _sha256(candidate)
    return None


def _radiated_band(doc: Mapping[str, Any]) -> tuple[float, float] | None:
    """The band this capture's DUT actually radiates, from its own curves.

    Absent yields ``None`` rather than a default span, for the reason
    :mod:`~jasper.audio_measurement.gate_disclosure`'s header records: the
    un-intersected band priced a tweeter from 357 Hz where it has no output
    and over-reported by 3x (E5, #1969).
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


def discover_captures(round_dir: Path) -> tuple[PoseCapture, ...]:
    """Every summed capture under ``round_dir``, bound to its own program.

    ``round_dir`` is a banked round directory (the one holding ``bundle/``)
    or the bundle itself. Raises :class:`RoundCapturesRefused` naming the
    missing input — an empty result is a finding, never an empty tuple. How
    MANY captures a reader needs is the reader's own bar: the sweep wants two
    poses, a close reference reads one.
    """
    round_dir = Path(round_dir)
    sidecars = sorted(round_dir.glob("**/summed/summed_*.json"))
    if not sidecars:
        raise RoundCapturesRefused(
            REFUSE_NO_CAPTURES,
            {"round_dir": str(round_dir), "looked_for": "**/summed/summed_*.json"},
        )
    programs: dict[str, Path] = {}
    for candidate in sorted(round_dir.glob("**/*program*.wav")):
        programs.setdefault(_sha256(candidate), candidate)
    if not programs:
        raise RoundCapturesRefused(
            REFUSE_NO_PROGRAMS,
            {"round_dir": str(round_dir), "looked_for": "**/*program*.wav"},
        )

    captures: list[PoseCapture] = []
    # Decoded once per unique program, not once per capture.
    program_audio: dict[str, tuple[np.ndarray, int]] = {}
    for sidecar in sidecars:
        try:
            doc = json.loads(sidecar.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise RoundCapturesRefused(
                REFUSE_CAPTURE_UNREADABLE,
                {"sidecar": sidecar.name, "detail": str(exc)},
            ) from exc
        wav = sidecar.with_suffix(".wav")
        if not wav.is_file():
            raise RoundCapturesRefused(
                REFUSE_CAPTURE_UNREADABLE,
                {"sidecar": sidecar.name, "detail": "no WAV beside the sidecar"},
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
        band = _radiated_band(doc)
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
        signal, rate = read_wav_mono(wav)
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
        captures.append(
            PoseCapture(
                capture_id=str(doc.get("position_id") or sidecar.stem),
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
            )
        )
    return tuple(sorted(captures, key=lambda cap: cap.capture_id))


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None
