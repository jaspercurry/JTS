# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Room or speaker: the same feature read through a ladder of gate windows.

A banked round's captures are deconvolved, gated at a ladder of window
lengths, and read pose by pose. What separates a room feature from a
loudspeaker one is **across-pose sigma that GROWS with window length**, not
sigma that is large: an azimuth-only pose cloud produces big, perfectly
window-invariant HF scatter that is pure directivity, and reading "high
sigma => room" mis-attributes it (issue #3495's 2026-09-01 amendment 2, from
``captures/recommission-day2-2026-09-01/p1-position-window/P1-REPORT.md``
§5b: 5.5x at 441.6 Hz and 5.1x at 1 kHz against 0.94x-1.4x above 2.5 kHz).

Three measured hazards this module exists to not repeat:

* **The window's own bias is not small and never vanishes.** A -4.5 dB,
  Q~17 notch at 441.6 Hz reads -1.3 dB at 7 ms and only -2.7 dB at 20 ms
  (P1 §5d). A raw long-rung delta therefore conflates the window with the
  room, so the published delta is null-model corrected: the fitted notch is
  synthesized, injected into a real capture IR, and re-read through the same
  rungs, and its own change is subtracted.
* **A number without its frame does not reproduce.** The same capture and
  the same feature read -6.25 dB in an ad-hoc frame and -1.07 dB under the
  gate ladder's own window shape (P1 §6). Every result carries the frame
  descriptor that produced it.
* **The pose label is not the pose** (#3503) **and the phase label is not
  the program** (#3504). Poses are keyed on the full declared
  (azimuth, elevation, distance) triple, never on a seat index at an assumed
  common height; captures are bound to programs by content hash, never by
  the sidecar's declared stimulus phase, which is mislabelled on five of six
  captures of the round this instrument was built from.

Pipeline stage: **diagnose**, offline and read-only. It plays nothing,
writes nothing but its own report, and decides nothing: the output is
evidence for an attribution argument, not a verdict, and EQ is not its
business.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from jasper.active_speaker.flat_spec import SPEC_BANDS
from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.deconv import regularized_deconvolution_full
from jasper.audio_measurement.gating import TAPER_FRACTION, build_gate_window
from jasper.audio_measurement.sweep import read_wav_mono

from .feature_classifier import (
    CENTRE_SEARCH_OCT,
    DETREND_FRACTION,
    MAGNITUDE_SMOOTH_FRACTION,
    PHASE_GATE_LEAD_MS,
    biquad_peaking,
    detrend,
    feature_q,
)

SCHEMA_VERSION = 1
GENERATED_BY = "jasper.active_speaker.crossover_v2.gate_sweep"

#: The ladder, shortest first. It reaches 20 ms deliberately: 441.6 Hz does
#: not have 5 cycles in a window until 12 ms and 358 Hz is not resolvable at
#: all below 12 ms, so a (3, 5, 7) ladder cannot price the contested band it
#: is being asked about. Short rungs test resolution validity; long rungs are
#: a deliberate room-admittance probe, and the two are never averaged.
DEFAULT_RUNGS_MS: tuple[float, ...] = (3.0, 4.0, 5.0, 7.0, 9.0, 12.0, 20.0)

#: Cycles-in-window bars. Below 2.5 the read is not resolution-valid (the
#: same 2.5/T the gate's own trusted floor is built on); below 5 it is grey.
#: Flags, not filters, on the published table — but ``2.5`` does bound which
#: rungs a sensitivity may be computed across.
RESOLUTION_INVALID_CYCLES = 2.5
RESOLUTION_GREY_CYCLES = 5.0

#: One normalisation constant per capture, from THIS band at THIS rung,
#: applied to every rung of that capture. Per-window normalisation would
#: poison exactly the cross-rung deltas this instrument publishes: it moved
#: the 441.6 Hz 7->20 ms read by 0.49 dB in P1 §6 on its own.
REFERENCE_BAND_HZ = (2500.0, 8000.0)
REFERENCE_RUNG_MS = 7.0

#: Analysis grid. Deliberately NOT :func:`.feature_classifier.analysis_grid`,
#: whose floor is 300 Hz: the lowest spec band starts at 250 Hz and the
#: features under investigation sit at 358 and 441.6 Hz, so the grid has to
#: reach below the band edge it grades.
GRID_LO_HZ = 200.0
GRID_HI_HZ = 20000.0
GRID_FRACTION = 48

#: Fixed for every rung and pose, so the smoother sees the same bin density
#: at 3 ms as at 20 ms. Zero-padding is interpolation, not resolution — the
#: real resolution limit is the window, which the cycles flags carry.
N_FFT = 1 << 16

#: Length of the null model's host array, in seconds. Long enough for a
#: high-Q notch's own ringing to finish well inside it.
NULL_MODEL_HOST_S = 0.2

# --- refusals: every one names the input that was missing --------------------

REFUSE_NO_CAPTURES = "gate_sweep_no_captures"
REFUSE_NO_PROGRAMS = "gate_sweep_no_programs"
REFUSE_PROGRAM_UNMATCHED = "gate_sweep_program_hash_unmatched"
REFUSE_RADIATED_BAND_MISSING = "gate_sweep_radiated_band_missing"
REFUSE_REFERENCE_BAND_EMPTY = "gate_sweep_reference_band_empty"
REFUSE_SINGLE_POSE = "gate_sweep_single_pose"
REFUSE_CAPTURE_UNREADABLE = "gate_sweep_capture_unreadable"

# --- why a band has no sensitivity -------------------------------------------

NULL_INSUFFICIENT_VALID_RUNGS = "insufficient_valid_rungs"
NULL_BAND_NOT_RADIATED = "band_outside_radiated_band"
NULL_DEGENERATE_SHORT_RUNG = "short_rung_sigma_is_zero"


class GateSweepRefused(Exception):
    """A named refusal with the evidence behind it. Never a bare failure."""

    def __init__(self, reason: str, detail: Mapping[str, Any]) -> None:
        super().__init__(f"{reason}: {json.dumps(detail, sort_keys=True, default=str)}")
        self.reason = reason
        self.detail = dict(detail)


@dataclass
class PoseCapture:
    """One banked capture, its declared pose, and its gated curves."""

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


def analysis_grid() -> np.ndarray:
    """Log grid at :data:`GRID_FRACTION` points per octave."""
    n = int(round(GRID_FRACTION * np.log2(GRID_HI_HZ / GRID_LO_HZ))) + 1
    return GRID_LO_HZ * 2.0 ** (np.arange(n) / GRID_FRACTION)


# --------------------------------------------------------------------------- #
# discovery — capture to program by content hash, never by label
# --------------------------------------------------------------------------- #


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
    or the bundle itself. Raises :class:`GateSweepRefused` naming the missing
    input — an empty result is a finding, never an empty tuple.
    """
    round_dir = Path(round_dir)
    sidecars = sorted(round_dir.glob("**/summed/summed_*.json"))
    if not sidecars:
        raise GateSweepRefused(
            REFUSE_NO_CAPTURES,
            {"round_dir": str(round_dir), "looked_for": "**/summed/summed_*.json"},
        )
    programs: dict[str, Path] = {}
    for candidate in sorted(round_dir.glob("**/*program*.wav")):
        programs.setdefault(_sha256(candidate), candidate)
    if not programs:
        raise GateSweepRefused(
            REFUSE_NO_PROGRAMS,
            {"round_dir": str(round_dir), "looked_for": "**/*program*.wav"},
        )

    captures: list[PoseCapture] = []
    for sidecar in sidecars:
        try:
            doc = json.loads(sidecar.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise GateSweepRefused(
                REFUSE_CAPTURE_UNREADABLE,
                {"sidecar": sidecar.name, "detail": str(exc)},
            ) from exc
        wav = sidecar.with_suffix(".wav")
        if not wav.is_file():
            raise GateSweepRefused(
                REFUSE_CAPTURE_UNREADABLE,
                {"sidecar": sidecar.name, "detail": "no WAV beside the sidecar"},
            )
        sha = _declared_program_sha(doc, round_dir)
        program = programs.get(sha) if sha is not None else None
        if program is None:
            raise GateSweepRefused(
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
            raise GateSweepRefused(
                REFUSE_RADIATED_BAND_MISSING,
                {
                    "sidecar": sidecar.name,
                    "note": (
                        "the reference band is intersected with the band the "
                        "DUT radiates; without it no honest band exists"
                    ),
                },
            )
        signal, rate = read_wav_mono(wav)
        program_signal, program_rate = read_wav_mono(program)
        if rate != program_rate:
            raise GateSweepRefused(
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
    if len(captures) < 2:
        raise GateSweepRefused(
            REFUSE_SINGLE_POSE,
            {
                "captures": [cap.capture_id for cap in captures],
                "note": "across-pose sigma needs at least two poses",
            },
        )
    return tuple(sorted(captures, key=lambda cap: cap.capture_id))


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


# --------------------------------------------------------------------------- #
# the window and the curve it produces
# --------------------------------------------------------------------------- #


def gated_segment(
    ir: np.ndarray,
    sample_rate: int,
    *,
    gate_ms: float,
    peak_idx: int,
    lead_ms: float = PHASE_GATE_LEAD_MS,
) -> tuple[np.ndarray, int]:
    """One rung's windowed segment, peak-aligned. Returns ``(segment, lead)``.

    The window is :func:`~jasper.audio_measurement.gating.build_gate_window`'s
    — the shipped gate's own shape at a forced span — with a 1.0 ms lead. The
    lead is load-bearing and measured: a zero-lead window truncates the direct
    arrival's own low-frequency pre-ringing and read 441.6 Hz at -14.6 dB
    against -2.5 dB with the lead (P1 §7).
    """
    span = int(round(gate_ms * 1e-3 * sample_rate))
    lead = int(round(lead_ms * 1e-3 * sample_rate))
    start = max(0, peak_idx - lead)
    lead = peak_idx - start
    want = lead + span + 1
    segment = np.asarray(ir[start : start + want], dtype=np.float64)
    if segment.size < want:
        segment = np.pad(segment, (0, want - segment.size))
    window = build_gate_window(
        want, peak_idx=lead, span=span, taper_fraction=TAPER_FRACTION, lead=lead
    )
    return segment * window, lead


def gated_curve(
    ir: np.ndarray,
    sample_rate: int,
    *,
    gate_ms: float,
    peak_idx: int,
    grid: np.ndarray,
) -> np.ndarray:
    """A rung's smoothed magnitude, in dB on ``grid``. Not normalised."""
    segment, _ = gated_segment(
        ir, sample_rate, gate_ms=gate_ms, peak_idx=peak_idx
    )
    spectrum = np.fft.rfft(segment, n=N_FFT)
    freqs = np.fft.rfftfreq(N_FFT, d=1.0 / sample_rate)
    db = 20.0 * np.log10(np.maximum(np.abs(spectrum), 1e-15))
    keep = (
        np.isfinite(db) & (freqs >= GRID_LO_HZ * 0.7) & (freqs <= GRID_HI_HZ * 1.3)
    )
    smoothed = smooth_fractional_octave(
        freqs[keep], db[keep], MAGNITUDE_SMOOTH_FRACTION
    )
    return np.interp(grid, freqs[keep], smoothed)


def _band_mean_db(
    curve: np.ndarray, grid: np.ndarray, band: tuple[float, float]
) -> float:
    mask = (grid >= band[0]) & (grid <= band[1])
    return float(np.mean(curve[mask])) if mask.any() else float("nan")


def _intersect(
    a: tuple[float, float], b: tuple[float, float]
) -> tuple[float, float] | None:
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    return (lo, hi) if lo < hi else None


def _read_curves(
    captures: Sequence[PoseCapture], grid: np.ndarray, rungs_ms: Sequence[float]
) -> None:
    """Fill every capture's normalised and detrended curves, in place."""
    for capture in captures:
        reference_band = _intersect(REFERENCE_BAND_HZ, capture.radiated_band_hz)
        if reference_band is None:
            raise GateSweepRefused(
                REFUSE_REFERENCE_BAND_EMPTY,
                {
                    "capture": capture.capture_id,
                    "radiated_band_hz": list(capture.radiated_band_hz),
                    "reference_band_hz": list(REFERENCE_BAND_HZ),
                    "note": "the reference band and the radiated band do not overlap",
                },
            )
        # The reference rung is computed whether or not it is a requested
        # rung: the constant has to be the same one on every ladder, or two
        # runs of this tool are not comparable.
        reference_curve = gated_curve(
            capture.ir,
            capture.sample_rate,
            gate_ms=REFERENCE_RUNG_MS,
            peak_idx=capture.peak_idx,
            grid=grid,
        )
        capture.reference_const_db = _band_mean_db(reference_curve, grid, reference_band)
        for rung in rungs_ms:
            raw = (
                reference_curve
                if rung == REFERENCE_RUNG_MS
                else gated_curve(
                    capture.ir,
                    capture.sample_rate,
                    gate_ms=rung,
                    peak_idx=capture.peak_idx,
                    grid=grid,
                )
            )
            capture.curves[rung] = raw - capture.reference_const_db
            capture.detrended[rung] = detrend(capture.curves[rung], grid)


# --------------------------------------------------------------------------- #
# the null model: what the WINDOW alone does to a feature of this shape
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NotchFit:
    """The band's worst feature, fitted across poses at the longest rung."""

    centre_hz: float
    depth_db: float
    q: float
    per_pose_centre_hz: tuple[float, ...]
    per_pose_depth_db: tuple[float, ...]
    per_pose_q: tuple[float, ...]


def fit_notch(
    captures: Sequence[PoseCapture],
    grid: np.ndarray,
    *,
    rung_ms: float,
    nominal_hz: float,
) -> NotchFit:
    """Centre, depth and Q of the feature at ``nominal_hz``, median of poses.

    The centre is searched over +/-:data:`.feature_classifier.CENTRE_SEARCH_OCT`
    (1/6 octave), NOT the classifier's 1/3-octave neighbourhood: the wider
    span walked off onto a different feature near 377 Hz on three of six
    poses (P1 §7) and fitted the null model to the wrong thing.
    """
    lo = nominal_hz * 2.0**-CENTRE_SEARCH_OCT
    hi = nominal_hz * 2.0**CENTRE_SEARCH_OCT
    mask = (grid >= lo) & (grid <= hi)
    centres: list[float] = []
    depths: list[float] = []
    qs: list[float] = []
    for capture in captures:
        curve = capture.detrended[rung_ms]
        index = int(np.argmax(np.abs(curve[mask])))
        centre = float(grid[mask][index])
        centres.append(centre)
        depths.append(float(curve[mask][index]))
        qs.append(feature_q(curve, grid, centre))
    return NotchFit(
        centre_hz=float(np.median(centres)),
        depth_db=float(np.median(depths)),
        q=float(np.median(qs)),
        per_pose_centre_hz=tuple(centres),
        per_pose_depth_db=tuple(depths),
        per_pose_q=tuple(qs),
    )


def null_model_hosts(
    capture: PoseCapture, *, host_rung_ms: float
) -> dict[str, tuple[np.ndarray, int]]:
    """The two hosts a synthesized notch is injected into, as ``(ir, peak)``.

    ``real`` is the capture's own IR pre-gated to ``host_rung_ms`` — the
    host the published correction uses, because a real IR carries the
    reflections and the noise floor a window actually acts on.

    ``synthetic`` is a bare impulse, and it is disclosed beside the real one
    for a measured reason: injecting a feature into a host that ALREADY has
    one at that frequency stops being additive once the pair is deep, and
    the two hosts then disagree (P1 §5d reports both for the same reason).
    A reader who sees them agree knows the correction is in its regime.
    """
    rate = capture.sample_rate
    segment, lead = gated_segment(
        capture.ir, rate, gate_ms=host_rung_ms, peak_idx=capture.peak_idx
    )
    n = max(int(NULL_MODEL_HOST_S * rate), segment.size + 1)
    real = np.zeros(n, dtype=np.float64)
    real[: segment.size] = segment
    synthetic = np.zeros(n, dtype=np.float64)
    synthetic[lead] = 1.0
    return {"real": (real, lead), "synthetic": (synthetic, lead)}


def window_bias_db(
    host: np.ndarray,
    sample_rate: int,
    *,
    peak_idx: int,
    grid: np.ndarray,
    fit: NotchFit,
    rungs_ms: Sequence[float],
) -> dict[float, float]:
    """How much of a feature of ``fit``'s shape each rung's WINDOW invents.

    The fitted notch is synthesized as a minimum-phase RBJ peaking section
    (:func:`.feature_classifier.biquad_peaking`), injected into ``host``, and
    re-read at every rung. Each read is with-notch minus without-notch
    through identical windows, so the host's own structure cancels and what
    is left is the window's doing.
    """
    from scipy.signal import lfilter

    b, a = biquad_peaking(fit.centre_hz, fit.depth_db, fit.q, sample_rate)
    clean = np.asarray(host, dtype=np.float64)
    notched = np.asarray(lfilter(b, a, clean), dtype=np.float64)

    bias: dict[float, float] = {}
    for rung in rungs_ms:
        with_notch = gated_curve(
            notched, sample_rate, gate_ms=rung, peak_idx=peak_idx, grid=grid
        )
        without = gated_curve(
            clean, sample_rate, gate_ms=rung, peak_idx=peak_idx, grid=grid
        )
        read_with = float(
            np.interp(fit.centre_hz, grid, detrend(with_notch, grid))
        )
        read_without = float(
            np.interp(fit.centre_hz, grid, detrend(without, grid))
        )
        bias[rung] = read_with - read_without
    return bias


# --------------------------------------------------------------------------- #
# the sweep
# --------------------------------------------------------------------------- #


def _cycles(freq_hz: float, rung_ms: float) -> float:
    return freq_hz * rung_ms * 1e-3


def _resolution(freq_hz: float, rung_ms: float) -> str:
    cycles = _cycles(freq_hz, rung_ms)
    if cycles < RESOLUTION_INVALID_CYCLES:
        return "invalid"
    if cycles < RESOLUTION_GREY_CYCLES:
        return "grey"
    return "ok"


def _across_pose_sigma(
    captures: Sequence[PoseCapture], rungs_ms: Sequence[float]
) -> dict[float, np.ndarray]:
    """Across-pose standard deviation of the normalised curves, per rung.

    On the NORMALISED curve, not the detrended one: one constant per capture
    means the spread is the poses disagreeing, and nothing else.
    """
    return {
        rung: np.std(
            np.array([cap.curves[rung] for cap in captures]), axis=0, ddof=1
        )
        for rung in rungs_ms
    }


def _band_result(
    captures: Sequence[PoseCapture],
    grid: np.ndarray,
    sigma: Mapping[float, np.ndarray],
    band: tuple[float, float, float],
    *,
    rungs_ms: Sequence[float],
) -> dict[str, Any]:
    lo_hz, hi_hz, tolerance_db = band
    radiated = (
        max(cap.radiated_band_hz[0] for cap in captures),
        min(cap.radiated_band_hz[1] for cap in captures),
    )
    graded = _intersect((lo_hz, hi_hz), radiated)
    result: dict[str, Any] = {
        "band_hz": [lo_hz, hi_hz],
        "tolerance_db": tolerance_db,
        "graded_band_hz": list(graded) if graded else None,
        "radiated_band_hz": list(radiated),
    }
    if graded is None:
        result["sensitivity"] = None
        result["sensitivity_null_reason"] = NULL_BAND_NOT_RADIATED
        return result

    mask = (grid >= graded[0]) & (grid < graded[1])
    longest = max(rungs_ms)
    median_detrended = np.median(
        np.array([cap.detrended[longest] for cap in captures]), axis=0
    )
    worst_index = int(np.argmax(np.abs(median_detrended[mask])))
    worst_hz = float(grid[mask][worst_index])
    grid_index = int(np.flatnonzero(mask)[worst_index])

    valid = [rung for rung in rungs_ms if _resolution(worst_hz, rung) != "invalid"]
    worst_sigma = {rung: float(sigma[rung][grid_index]) for rung in rungs_ms}
    result.update(
        {
            "worst_bin_hz": worst_hz,
            "cycles_by_rung": {_key(r): _cycles(worst_hz, r) for r in rungs_ms},
            "resolution_by_rung": {_key(r): _resolution(worst_hz, r) for r in rungs_ms},
            "sigma_db_by_rung": {_key(r): worst_sigma[r] for r in rungs_ms},
            "n_valid_rungs": len(valid),
            "valid_rungs_ms": list(valid),
            # The band's deepest feature is not always its most window-
            # divergent one (on r9-verify-clean the deepest bin in 250-2000 Hz
            # grows 1.9x while 442 Hz in the same band grows 3.5x), so the
            # whole band's mean sigma is published beside the worst bin's.
            # It pools every graded bin at every rung, including bins below
            # their own resolution floor at the short rungs: that is P1 §5b's
            # statistic, and the frame's resolution bars price it. No ratio is
            # published for it — a ratio of two sigmas over 700 bins is set by
            # its smallest denominator, not by the room.
            "band_mean_sigma_db_by_rung": {
                _key(r): float(np.mean(sigma[r][mask])) for r in rungs_ms
            },
            "poses": [
                {
                    "pose_key": cap.pose_key,
                    "capture_id": cap.capture_id,
                    "azimuth_deg": cap.azimuth_deg,
                    "vertical_deg": cap.vertical_deg,
                    "mark_distance_m": cap.mark_distance_m,
                    "value_db_by_rung": {
                        _key(r): float(cap.curves[r][grid_index]) for r in rungs_ms
                    },
                    "detrended_db_by_rung": {
                        _key(r): float(cap.detrended[r][grid_index]) for r in rungs_ms
                    },
                }
                for cap in captures
            ],
        }
    )
    if len(valid) < 2:
        result["sensitivity"] = None
        result["sensitivity_null_reason"] = NULL_INSUFFICIENT_VALID_RUNGS
        return result

    short, long_ = min(valid), max(valid)
    if worst_sigma[short] <= 0.0:
        result["sensitivity"] = None
        result["sensitivity_null_reason"] = NULL_DEGENERATE_SHORT_RUNG
        return result

    fit = fit_notch(captures, grid, rung_ms=long_, nominal_hz=worst_hz)
    host = captures[0]
    hosts = null_model_hosts(host, host_rung_ms=long_)
    bias_by_host = {
        name: window_bias_db(
            ir,
            host.sample_rate,
            peak_idx=peak,
            grid=grid,
            fit=fit,
            rungs_ms=(short, long_),
        )
        for name, (ir, peak) in hosts.items()
    }
    bias = bias_by_host["real"]
    raw_delta = float(
        np.median([cap.detrended[long_][grid_index] for cap in captures])
        - np.median([cap.detrended[short][grid_index] for cap in captures])
    )
    bias_delta = bias[long_] - bias[short]
    synthetic = bias_by_host["synthetic"]
    synthetic_delta = synthetic[long_] - synthetic[short]
    result["sensitivity"] = {
        "shortest_valid_rung_ms": short,
        "longest_valid_rung_ms": long_,
        "sigma_growth_ratio": worst_sigma[long_] / worst_sigma[short],
        "raw_delta_db": raw_delta,
        "bias_delta_db": bias_delta,
        "corrected_delta_db": raw_delta - bias_delta,
        # The same bias through a bare-impulse host. It corrects nothing —
        # it is the disclosure that says whether the real host was still
        # additive at this depth (see :func:`null_model_hosts`).
        "bias_delta_synthetic_host_db": synthetic_delta,
        "null_model": {
            "centre_hz": fit.centre_hz,
            "depth_db": fit.depth_db,
            "q": fit.q,
            "per_pose_centre_hz": list(fit.per_pose_centre_hz),
            "per_pose_depth_db": list(fit.per_pose_depth_db),
            "per_pose_q": list(fit.per_pose_q),
            "host_capture_id": host.capture_id,
            "read_db_by_rung": {_key(r): bias[r] for r in (short, long_)},
            "synthetic_host_read_db_by_rung": {
                _key(r): synthetic[r] for r in (short, long_)
            },
        },
    }
    result["sensitivity_null_reason"] = None
    return result


def _key(rung_ms: float) -> str:
    return f"{rung_ms:g}"


def frame_descriptor(rungs_ms: Sequence[float], grid: np.ndarray) -> dict[str, Any]:
    """The frame every number in this report is stated in (#3495 amendment 3).

    Same capture, same feature, four defensible frames: -6.25, -3.50, -2.79
    and -1.07 dB (P1 §6). A sensitivity without its frame is the frame's
    number, not the room's.
    """
    return {
        "window": {
            "owner": "jasper.audio_measurement.gating.build_gate_window",
            "kind": "rect_head_flat_plateau_half_hann_tail",
            "taper_fraction": TAPER_FRACTION,
            "lead_ms": PHASE_GATE_LEAD_MS,
            "lead_shape": "raised_cosine",
        },
        "rungs_ms": list(rungs_ms),
        "smoothing": {
            "magnitude_fraction": MAGNITUDE_SMOOTH_FRACTION,
            "detrend_fraction": DETREND_FRACTION,
            "kind": "power_mean_fractional_octave",
        },
        "grid": {
            "lo_hz": GRID_LO_HZ,
            "hi_hz": GRID_HI_HZ,
            "fraction": GRID_FRACTION,
            "points": int(grid.size),
        },
        "n_fft": N_FFT,
        "reference": {
            "policy": "one constant per capture, applied to every rung",
            "band_hz": list(REFERENCE_BAND_HZ),
            "rung_ms": REFERENCE_RUNG_MS,
            "statistic": "arithmetic mean of the smoothed dB curve",
            "intersected_with_radiated_band": True,
        },
        "deconvolution": "jasper.audio_measurement.deconv.regularized_deconvolution_full",
        "direct_peak": "integer argmax(|ir|)",
        "centre_search_oct": CENTRE_SEARCH_OCT,
        "resolution_bars_cycles": {
            "invalid_below": RESOLUTION_INVALID_CYCLES,
            "grey_below": RESOLUTION_GREY_CYCLES,
        },
    }


def sweep_round(
    round_dir: Path, *, rungs_ms: Sequence[float] = DEFAULT_RUNGS_MS
) -> dict[str, Any]:
    """Sweep one banked round's gate and report what moved with the window.

    Raises :class:`GateSweepRefused` naming the missing input.
    """
    rungs = tuple(sorted(float(rung) for rung in rungs_ms))
    if len(rungs) < 2:
        raise ValueError("a gate sweep needs at least two rungs")
    captures = discover_captures(Path(round_dir))
    grid = analysis_grid()
    _read_curves(captures, grid, rungs)
    sigma = _across_pose_sigma(captures, rungs)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_by": GENERATED_BY,
        "round_dir": str(Path(round_dir)),
        "frame": frame_descriptor(rungs, grid),
        "poses": [
            {
                "pose_key": cap.pose_key,
                "capture_id": cap.capture_id,
                "phase": cap.phase,
                "azimuth_deg": cap.azimuth_deg,
                "vertical_deg": cap.vertical_deg,
                "mark_distance_m": cap.mark_distance_m,
                "capture_wav": cap.wav.name,
                "program_wav": cap.program.name,
                "program_sha256_12": cap.program_sha256[:12],
                "sample_rate_hz": cap.sample_rate,
                "direct_peak_ms": 1000.0 * cap.peak_idx / cap.sample_rate,
                "reference_const_db": cap.reference_const_db,
                "radiated_band_hz": list(cap.radiated_band_hz),
            }
            for cap in captures
        ],
        "bands": [
            _band_result(captures, grid, sigma, band, rungs_ms=rungs)
            for band in SPEC_BANDS
        ],
    }
