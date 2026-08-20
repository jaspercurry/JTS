# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The round-grading comparison views a laptop campaign had re-derived by hand.

Product promotion of four tools that lived under
``captures/linearization-night-2026-08-19/tools/`` — ``frozen_reference.py``,
``per_seat.py``, ``repeatability.py``, and ``agreement.py`` — which computed
the numbers the crossover-v2 tournament's "dominance decides" rule was
actually judged on (issue #2769). This module is the *reading and grading*
half of that campaign; every DSP and grading primitive it uses is imported
from the seam that already owns it:

* :mod:`~jasper.active_speaker.crossover_v2.evidence_packet` reads a banked
  round's bundle (this module never globs ``cloud_verify.json`` by hand).
* :mod:`~jasper.active_speaker.flat_spec` grades a curve
  (:func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec`); this module
  never re-derives a reference level or a band tolerance.
* :mod:`~jasper.active_speaker.flat_spec_views` re-reads an already-graded
  report per position/role (:func:`~jasper.active_speaker.flat_spec_views._evaluate_position`,
  :func:`~jasper.active_speaker.flat_spec_views._pool`) — the same building
  blocks the campaign's own ``frozen_reference.py`` used, imported here rather
  than re-implemented.
* :mod:`~jasper.audio_measurement.deconv` (``deconvolve``,
  ``magnitude_response``), :mod:`~jasper.audio_measurement.gating`
  (``gate_impulse_response``, the reflection-detecting gate — NOT the
  campaign's fixed 7 ms Hann window), and
  :mod:`~jasper.audio_measurement.analysis` (``smooth_fractional_octave``)
  are the only DSP this module performs itself, and only for the one curve a
  round's bundle does not already carry pre-computed: the VERIFY pose.

**Input shape**: a *banked round directory*, the tree
``scripts/bank-crossover-round.sh <dest-dir>`` produces (PR #2778) —

.. code-block:: text

    <round-dir>/
      bundle/<session-id>/...        one active-speaker session bundle
      state.json                     crossover-v2 flow state (optional)
      design-draft.json              active-speaker design draft (optional)
      dumps/wav/*.wav                dump-ring captures (optional)
      dumps/sidecar/*.json           dump-ring sidecars, one per wav (optional)

**What this module deliberately does NOT do.** No numeric microphone angle
is recovered for any position. :mod:`.evidence_packet` already made and
documented that call — a round's bundle carries a position's ``role``
(``onax``/``offax``), never a degree, and recovering one means reading a
walk-driver log that is not part of any banked round. The campaign's
``frozen_reference.py`` carried a hardcoded ``index -> degrees`` table for
exactly this reason; it is not ported. Every view here keys a position by
its own stable ``position_id`` instead (``f"{phase}_{index:02d}"``, assigned
once by the walk driver and stable across rounds that walk the same shape).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.active_speaker import flat_spec
from jasper.active_speaker.flat_spec import BandResult, FlatSpecReport, evaluate_flat_spec
from jasper.active_speaker.flat_spec_views import (
    PositionCurve,
    _evaluate_position,
    _pool,
)
from jasper.active_speaker.crossover_v2.evidence_packet import (
    CrossoverEvidencePacketError,
    build_crossover_evidence_packet,
)
from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.deconv import deconvolve, magnitude_response
from jasper.audio_measurement.gating import gate_impulse_response
from jasper.audio_measurement.sweep import read_wav_mono

__all__ = [
    "AgreementFeature",
    "BankedRound",
    "FrozenReferenceResult",
    "RepeatabilityMetric",
    "RepeatabilityResult",
    "RoundViewsError",
    "SeatCurve",
    "VerifyPoseResult",
    "agreement_table",
    "frozen_reference_grade",
    "load_banked_round",
    "per_seat_curves",
    "repeatability_spread",
    "verify_pose_curve",
]

#: Mirrors ``crossover_v2_flow.POSITION_ROLE_ONAX`` as a local literal rather
#: than importing that (large, orchestration-heavy) module for one string.
#: :mod:`.flat_spec_views` follows the same policy for the same reason — see
#: its ``PositionCurve.role`` docstring: this package never owns that
#: constant, it only takes it as a caller-supplied value.
DEFAULT_PRIMARY_ROLE = "onax"

#: The synthetic role/position-id this module mints for a VERIFY-phase
#: capture, which a round's bundle never carries a ``positions`` row for.
VERIFY_ROLE = "verify"
VERIFY_POSITION_ID = "verify"

#: Where a session bundle keeps its round artifacts, mirroring
#: :mod:`.evidence_packet`'s own ``_EVIDENCE_GLOB`` — duplicated as a literal
#: rather than imported because the private name is that module's own.
_EVIDENCE_GLOB = "evidence/v1/artifacts/crossover_v2/*"


class RoundViewsError(ValueError):
    """A banked round directory could not be read into a comparable view."""


# --------------------------------------------------------------------------- #
# Loading one banked round
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BankedRound:
    """One banked round, read once, ready for every view below.

    ``report`` carries the round's own grading frame (trusted floor,
    published exclusion intervals, and — when the persisted ``spec`` block
    has bands — the full per-band evaluation, needed for
    :func:`repeatability_spread`'s pooled figures). ``positions`` is built
    directly from the evidence packet's ``positions`` block; nothing here
    re-parses ``cloud_verify.json``.
    """

    round_dir: Path
    session_dir: Path
    positions: tuple[PositionCurve, ...]
    curve_grid_hz: np.ndarray
    report: FlatSpecReport
    packet: Mapping[str, Any] = field(repr=False)


def _bundle_session_dir(round_dir: Path) -> Path:
    bundle_dir = round_dir / "bundle"
    if not bundle_dir.is_dir():
        raise RoundViewsError(f"{round_dir}: no bundle/ directory (did bank-crossover-round.sh run?)")
    children = sorted(p for p in bundle_dir.iterdir() if p.is_dir())
    if len(children) != 1:
        raise RoundViewsError(
            f"{bundle_dir}: expected exactly one session directory, found {len(children)}"
        )
    return children[0]


def _band_result_from_dict(raw: Mapping[str, Any]) -> BandResult:
    """One persisted band back into its dataclass, field for field.

    Mirrors ``scripts/render-metric-views.py``'s ``_band_result_from_dict`` —
    the same rehydration of ``BandResult.to_dict()``'s own shape. Kept local
    rather than imported because that script has no importable package
    surface (it is a lab CLI, not a module); not promoted onto
    :class:`~jasper.active_speaker.flat_spec.BandResult` itself because this
    PR's scope is the four campaign views, not a refactor of the safety-
    reviewed spec module's serialization contract.
    """
    return BandResult(
        f_lo_hz=float(raw["f_lo_hz"]),
        f_hi_hz=float(raw["f_hi_hz"]),
        tolerance_db=float(raw["tolerance_db"]),
        max_deviation_db=raw.get("max_deviation_db"),
        max_deviation_hz=raw.get("max_deviation_hz"),
        rms_deviation_db=raw.get("rms_deviation_db"),
        n_bins=int(raw["n_bins"]),
        n_excluded=int(raw["n_excluded"]),
        evaluable=bool(raw["evaluable"]),
        passed=raw.get("passed"),
        level_deviation_db=raw.get("level_deviation_db"),
        max_ripple_db=raw.get("max_ripple_db"),
        max_ripple_hz=raw.get("max_ripple_hz"),
        graded_lo_hz=raw.get("graded_lo_hz"),
        max_at_graded_edge=raw.get("max_at_graded_edge"),
    )


def _report_from_spec_dict(spec: Mapping[str, Any]) -> FlatSpecReport:
    """The packet's ``spec`` block back into a :class:`FlatSpecReport`.

    The packet copies ``cloud_verify.json``'s ``spec`` verbatim
    (:func:`~jasper.active_speaker.crossover_v2.evidence_packet.build_crossover_evidence_packet`),
    which is itself ``FlatSpecReport.to_dict()`` — so this is a rehydration,
    not a re-derivation. No band edge, floor, or reference is recomputed.
    """
    reference_band = spec.get("reference_band_hz")
    kwargs: dict[str, Any] = {}
    if reference_band is not None:
        kwargs["reference_band_hz"] = (float(reference_band[0]), float(reference_band[1]))
    return FlatSpecReport(
        reference_db=float(spec["reference_db"]),
        bands=tuple(_band_result_from_dict(b) for b in spec["bands"]),
        overall_passed=bool(spec["overall_passed"]),
        excluded_intervals=tuple(
            (float(lo), float(hi)) for lo, hi in spec.get("excluded_intervals", ())
        ),
        best_effort_above_hz=float(spec["best_effort_above_hz"]),
        smoothing_fraction=int(spec["smoothing_fraction"]),
        trusted_floor_hz=spec.get("trusted_floor_hz"),
        **kwargs,
    )


def load_banked_round(round_dir: Path) -> BankedRound:
    """Read one ``bank-crossover-round.sh`` output directory into a
    :class:`BankedRound`.

    Raises :class:`RoundViewsError` when the directory is not a usable
    banked round — no bundle, no readable evidence packet, or a packet
    carrying no position evidence / no graded spec. Every one of the four
    views below needs both, so failing loudly here is more useful than four
    separate partial failures downstream.
    """
    round_dir = Path(round_dir)
    session_dir = _bundle_session_dir(round_dir)
    state_path = round_dir / "state.json"
    draft_path = round_dir / "design-draft.json"
    try:
        packet = build_crossover_evidence_packet(
            session_dir,
            state_path=state_path if state_path.is_file() else None,
            driver_draft_path=draft_path if draft_path.is_file() else None,
        )
    except CrossoverEvidencePacketError as exc:
        raise RoundViewsError(f"{round_dir}: {exc}") from exc

    positions_block = packet.get("positions") or {}
    if not positions_block.get("available"):
        raise RoundViewsError(f"{round_dir}: evidence packet carries no position evidence")
    spec_block = packet.get("spec") or {}
    if not spec_block.get("bands"):
        raise RoundViewsError(f"{round_dir}: evidence packet carries no graded spec")

    grid = np.asarray(positions_block["curve_grid"].get("freqs_hz") or [], dtype=float)
    smoothing = int(positions_block["curve_grid"].get("smoothing_fraction") or 0)
    positions = tuple(
        PositionCurve(
            position_id=str(row.get("position_id") or ""),
            role=str(row.get("role") or ""),
            freqs_hz=grid,
            magnitude_db=np.asarray(row.get("magnitude_db") or [], dtype=float),
            smoothing_fraction=smoothing,
            degrees=None,
            take_id=str(row.get("take_id") or ""),
        )
        for row in positions_block.get("positions") or []
        if row.get("magnitude_db")
    )
    if not positions:
        raise RoundViewsError(f"{round_dir}: every position row is missing its magnitude_db")

    report = _report_from_spec_dict(spec_block)
    return BankedRound(
        round_dir=round_dir,
        session_dir=session_dir,
        positions=positions,
        curve_grid_hz=grid,
        report=report,
        packet=packet,
    )


# --------------------------------------------------------------------------- #
# View 1 — frozen-reference grading through the shipped grader
# --------------------------------------------------------------------------- #


def _own_reference_db(position: PositionCurve, report: FlatSpecReport) -> float:
    """The reference the SHIPPED path grades this position against — read
    back off the real evaluator, never recomputed here."""
    graded = evaluate_flat_spec(
        np.asarray(position.freqs_hz, dtype=float),
        np.asarray(position.magnitude_db, dtype=float),
        _mask_from_intervals(position.freqs_hz, report.excluded_intervals),
        smoothing_fraction=position.smoothing_fraction,
        trusted_floor_hz=report.trusted_floor_hz,
    )
    return float(graded.reference_db)


def _mask_from_intervals(
    freqs_hz: np.ndarray, intervals: tuple[tuple[float, float], ...]
) -> np.ndarray:
    freqs = np.asarray(freqs_hz, dtype=float)
    mask = np.zeros(freqs.size, dtype=bool)
    for lo, hi in intervals:
        mask |= (freqs >= lo) & (freqs <= hi)
    return mask


class _FrozenReference:
    """Substitute ``reference_db`` for ONE ``evaluate_flat_spec`` call.

    Ported verbatim from the campaign's ``frozen_reference.py`` (self-tested
    there against the linearization campaign's own §8.9 table). The first
    call to the patched helper returns the frozen value; every later call in
    the same evaluation delegates to the real helper so band levels stay
    genuine. The substitution point is
    :func:`jasper.active_speaker.flat_spec._power_mean_db`, which
    ``evaluate_flat_spec`` calls FIRST to build ``reference_db`` and only
    afterwards, inside the band loop, to build each band's own level — so
    patching only the first call is the whole trick. Landing is asserted by
    the caller (:func:`frozen_reference_grade`), never trusted silently.
    """

    def __init__(self, value: float) -> None:
        self.value = value
        self.calls = 0
        self._real = flat_spec._power_mean_db

    def __enter__(self) -> "_FrozenReference":
        def patched(values_db: np.ndarray) -> float:
            self.calls += 1
            if self.calls == 1:
                return self.value
            return self._real(values_db)

        flat_spec._power_mean_db = patched
        return self

    def __exit__(self, *exc: object) -> None:
        flat_spec._power_mean_db = self._real


def _grade_positions(
    positions: tuple[PositionCurve, ...],
    report: FlatSpecReport,
    frozen_refs: Mapping[str, float] | None,
) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    """One grading pass — shipped when ``frozen_refs`` is ``None``, frozen to
    the supplied per-position references otherwise.

    Returns ``(per_role_pooled, per_position_rms_db)``.
    """
    per_role: dict[str, list[tuple[float, float]]] = {}
    per_position: dict[str, float] = {}
    for position in positions:
        seat = position.position_id
        if frozen_refs is None:
            flatness, _octaves = _evaluate_position(position, report)
        else:
            with _FrozenReference(frozen_refs[seat]) as patch:
                flatness, _octaves = _evaluate_position(position, report)
            if patch.calls < 1:
                raise RoundViewsError(
                    f"{seat}: frozen reference never consumed — the evaluator "
                    "did not call _power_mean_db"
                )
        if not flatness.evaluable or flatness.rms_db is None:
            raise RoundViewsError(f"{seat}: position not evaluable under this report's frame")
        per_position[seat] = float(flatness.rms_db)
        per_role.setdefault(position.role, []).append((float(flatness.n_bins), float(flatness.rms_db)))
    pooled = {role: value for role, pairs in per_role.items() if (value := _pool(pairs)) is not None}
    return pooled, per_position


@dataclass(frozen=True)
class FrozenReferenceResult:
    """One target round graded twice: as shipped, and frozen to the
    baseline's per-position reference levels.

    ``shipped`` and ``frozen`` are ``{role: pooled_rms_db}``. The freeze
    removes exactly the one degree of freedom §8.9 found compensating a
    prescribed cut's level loss (grading each config against its OWN
    reference) — see the module docstring's linked campaign tool for the
    full derivation. ``target_own_refs`` / ``baseline_refs`` are the
    per-position reference levels each half actually used, so a caller can
    audit the freeze rather than trust it.
    """

    baseline_round_dir: str
    target_round_dir: str
    shipped: dict[str, float]
    frozen: dict[str, float]
    shipped_positions: dict[str, float]
    frozen_positions: dict[str, float]
    baseline_refs: dict[str, float]
    target_own_refs: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_round_dir": self.baseline_round_dir,
            "target_round_dir": self.target_round_dir,
            "shipped": self.shipped,
            "frozen": self.frozen,
            "shipped_positions": self.shipped_positions,
            "frozen_positions": self.frozen_positions,
            "baseline_refs": self.baseline_refs,
            "target_own_refs": self.target_own_refs,
        }


def frozen_reference_grade(baseline: BankedRound, target: BankedRound) -> FrozenReferenceResult:
    """Grade ``target`` twice: shipped, and frozen to ``baseline``'s
    per-position reference levels.

    ``target`` may be the same round as ``baseline`` (frozen == shipped by
    construction in that case — the freeze changes nothing when a round is
    compared against itself). Raises :class:`RoundViewsError` when a
    position present in ``target`` has no counterpart
    (matched by ``position_id``) in ``baseline``, or when any position is
    not evaluable under its own report's frame.
    """
    baseline_pooled, baseline_positions = _grade_positions(baseline.positions, baseline.report, None)
    baseline_refs = {
        position.position_id: _own_reference_db(position, baseline.report)
        for position in baseline.positions
    }
    missing = [p.position_id for p in target.positions if p.position_id not in baseline_refs]
    if missing:
        raise RoundViewsError(
            f"target round has position(s) {missing} with no baseline counterpart "
            f"(baseline has {sorted(baseline_refs)})"
        )
    target_own_refs = {
        position.position_id: _own_reference_db(position, target.report)
        for position in target.positions
    }
    shipped_pooled, shipped_positions = _grade_positions(target.positions, target.report, None)
    frozen_pooled, frozen_positions = _grade_positions(target.positions, target.report, baseline_refs)
    return FrozenReferenceResult(
        baseline_round_dir=str(baseline.round_dir),
        target_round_dir=str(target.round_dir),
        shipped=shipped_pooled,
        frozen=frozen_pooled,
        shipped_positions=shipped_positions,
        frozen_positions=frozen_positions,
        baseline_refs=baseline_refs,
        target_own_refs=target_own_refs,
    )


# --------------------------------------------------------------------------- #
# View 2 — the VERIFY pose made comparable to the cloud positions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VerifyPoseResult:
    """The VERIFY-phase (on-axis, zero-degree by definition of the phase)
    capture, deconvolved against its own program and put on the round's
    ``curve_grid_hz`` — or the reason it could not be.

    ``curve`` is ``None`` exactly when ``reason`` is non-empty. Never
    raises: a round banked without dump-ring captures enabled, or with no
    VERIFY-phase capture in the ring, is a normal, expected shape (dump-ring
    retention is opt-in), not an error.
    """

    curve: PositionCurve | None
    n_captures: int
    reason: str


def _dump_ring_captures(round_dir: Path, *, phase: str) -> list[tuple[Path, Path]]:
    """``(wav_path, sidecar_path)`` pairs whose sidecar reports ``phase``."""
    sidecar_dir = round_dir / "dumps" / "sidecar"
    wav_dir = round_dir / "dumps" / "wav"
    if not sidecar_dir.is_dir() or not wav_dir.is_dir():
        return []
    pairs: list[tuple[Path, Path]] = []
    for sidecar_path in sorted(sidecar_dir.glob("*.json")):
        try:
            doc = json.loads(sidecar_path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict) or doc.get("phase") != phase:
            continue
        wav_path = wav_dir / (sidecar_path.stem + ".wav")
        if wav_path.is_file():
            pairs.append((wav_path, sidecar_path))
    return pairs


def _find_program_wav(session_dir: Path, *, phase: str) -> Path | None:
    for relay_dir in sorted(session_dir.glob(_EVIDENCE_GLOB)):
        candidate = relay_dir / f"{phase}_program.wav"
        if candidate.is_file():
            return candidate
    return None


def verify_pose_curve(
    banked: BankedRound, *, phase: str = "verify", smoothing_fraction: int | None = None
) -> VerifyPoseResult:
    """Deconvolve every ``phase``-tagged dump-ring capture against its own
    banked program, gate, smooth, and resample onto ``banked.curve_grid_hz``.

    Multiple captures are averaged in dB (matching the campaign tool's own
    per-seat averaging). ``smoothing_fraction`` defaults to the round's own
    (``banked.report.smoothing_fraction``) so the result is smoothed the
    same as the cloud positions it will be compared against — a different
    fraction would make a shape difference an artifact of the comparison
    rather than the speaker.
    """
    program_path = _find_program_wav(banked.session_dir, phase=phase)
    if program_path is None:
        return VerifyPoseResult(None, 0, f"no {phase}_program.wav banked in the round's bundle")
    captures = _dump_ring_captures(banked.round_dir, phase=phase)
    if not captures:
        return VerifyPoseResult(
            None, 0, f"no dump-ring capture tagged phase={phase!r} under dumps/"
        )
    fraction = smoothing_fraction if smoothing_fraction is not None else banked.report.smoothing_fraction
    if fraction <= 0:
        fraction = 12

    program, program_sr = read_wav_mono(program_path)
    grid = np.asarray(banked.curve_grid_hz, dtype=float)
    curves = []
    for wav_path, _sidecar_path in captures:
        captured, sr = read_wav_mono(wav_path)
        if sr != program_sr:
            continue
        ir = deconvolve(captured, program, sr)
        gated_ir, _fragment = gate_impulse_response(ir, sr)
        freqs_lin, mag_db_lin = magnitude_response(gated_ir, sr)
        mag_smoothed = smooth_fractional_octave(freqs_lin, mag_db_lin, fraction=fraction)
        curves.append(np.interp(grid, freqs_lin, mag_smoothed))
    if not curves:
        return VerifyPoseResult(
            None, 0, f"every phase={phase!r} capture's sample rate disagreed with its program"
        )
    averaged = np.mean(np.vstack(curves), axis=0)
    curve = PositionCurve(
        position_id=VERIFY_POSITION_ID,
        role=VERIFY_ROLE,
        freqs_hz=grid,
        magnitude_db=averaged,
        smoothing_fraction=fraction,
        # The VERIFY phase measures the confirmed on-axis listening position
        # by definition of the phase — this is not an angle recovered from a
        # walk log (none exists for this pose), it is what the phase means.
        degrees=0.0,
        take_id="",
    )
    return VerifyPoseResult(curve, len(curves), "")


# --------------------------------------------------------------------------- #
# View 3 — every seat comparable, including one a round has no position row for
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SeatCurve:
    """One position's (or the VERIFY pose's) curve, normalised against its
    own median level over ``norm_band_hz`` — so a level difference between
    rounds or pipelines cannot masquerade as a shape difference."""

    position_id: str
    role: str
    normalized_db: np.ndarray


def per_seat_curves(
    banked: BankedRound,
    verify: PositionCurve | None = None,
    *,
    norm_band_hz: tuple[float, float] = (400.0, 8000.0),
) -> tuple[SeatCurve, ...]:
    """Every banked position plus, when supplied, the VERIFY pose — all
    normalised onto a comparable basis.

    Each curve is expressed as its own deviation from its own median level
    over ``norm_band_hz`` (the campaign tool's own normalisation). This is
    what makes the VERIFY pose — captured through an entirely different DSP
    path than the banked cloud positions — comparable to them without any
    cross-calibration assumption: only SHAPE is compared, never absolute
    level.
    """
    grid = np.asarray(banked.curve_grid_hz, dtype=float)
    sel = (grid >= norm_band_hz[0]) & (grid <= norm_band_hz[1])
    if not np.any(sel):
        raise RoundViewsError(f"norm band {norm_band_hz} has no bins on this round's curve grid")

    def _seat(position_id: str, role: str, curve_db: np.ndarray) -> SeatCurve:
        curve_db = np.asarray(curve_db, dtype=float)
        return SeatCurve(position_id, role, curve_db - float(np.median(curve_db[sel])))

    seats = [_seat(p.position_id, p.role, p.magnitude_db) for p in banked.positions]
    if verify is not None:
        seats.append(_seat(verify.position_id, verify.role, verify.magnitude_db))
    return tuple(seats)


# --------------------------------------------------------------------------- #
# View 4 — session-to-session repeatability of the honest pooled figures
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RepeatabilityMetric:
    """One metric's spread across the compared rounds, plus each round's own
    value keyed by the round label the caller supplied."""

    name: str
    values: dict[str, float]

    def spread(self) -> dict[str, float] | None:
        vs = list(self.values.values())
        if len(vs) < 2:
            return None
        mean = sum(vs) / len(vs)
        variance = sum((v - mean) ** 2 for v in vs) / (len(vs) - 1)
        return {
            "n": float(len(vs)),
            "mean": mean,
            "range": max(vs) - min(vs),
            "sd": variance**0.5,
            "min": min(vs),
            "max": max(vs),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "values": self.values, "spread": self.spread()}


@dataclass(frozen=True)
class RepeatabilityResult:
    """The stop-criterion table: per-round pooled figures, their spread
    (the measured repeat noise), and per-position stability."""

    round_labels: tuple[str, ...]
    metrics: tuple[RepeatabilityMetric, ...]
    per_position: tuple[RepeatabilityMetric, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_labels": list(self.round_labels),
            "metrics": [m.to_dict() for m in self.metrics],
            "per_position": [m.to_dict() for m in self.per_position],
        }


def repeatability_spread(
    rounds: Sequence[tuple[str, BankedRound]], *, primary_role: str = DEFAULT_PRIMARY_ROLE
) -> RepeatabilityResult:
    """Session-to-session spread of the pooled honest figures, plus each
    round's own value and the pairwise deltas a caller can derive from them.

    ``rounds`` is ``(label, banked_round)`` pairs — the label is whatever
    the caller wants printed (a session id, a timestamp, an attempt name).
    Each round is graded SHIPPED (no frozen substitution) via the same
    :func:`~jasper.active_speaker.flat_spec_views._evaluate_position` /
    :func:`~jasper.active_speaker.flat_spec_views._pool` building blocks
    :func:`frozen_reference_grade` uses, so the two views agree on the
    shipped number by construction.
    """
    role_pooled: dict[str, dict[str, float]] = {}
    linear_pooled: dict[str, float] = {}
    position_values: dict[str, dict[str, float]] = {}
    for label, banked in rounds:
        pooled, per_position = _grade_positions(banked.positions, banked.report, None)
        for role, value in pooled.items():
            role_pooled.setdefault(role, {})[label] = value
        for seat, value in per_position.items():
            position_values.setdefault(seat, {})[label] = value
        # The SHIPPED linear-pooled figure — spec_convergence_residual's own
        # number, lifted from the report rather than recomputed — carried
        # beside the per-octave/per-role re-poolings above so a caller can
        # see whether the number the tournament actually reads repeats too.
        residual = flat_spec.spec_convergence_residual(banked.report)
        if residual.evaluable and residual.rms_db is not None:
            linear_pooled[label] = float(residual.rms_db)

    labels = tuple(label for label, _banked in rounds)
    metrics = [RepeatabilityMetric("shipped_linear_pool_db", dict(linear_pooled))]
    for role in sorted(role_pooled):
        name = f"{role}_log_pooled_db" if role != primary_role else f"{role}_log_pooled_db(primary)"
        metrics.append(RepeatabilityMetric(name, dict(role_pooled[role])))
    per_position = [
        RepeatabilityMetric(seat, dict(values)) for seat, values in sorted(position_values.items())
    ]
    return RepeatabilityResult(round_labels=labels, metrics=tuple(metrics), per_position=tuple(per_position))


# --------------------------------------------------------------------------- #
# Agreement — per-seat sign/magnitude testimony for every feature
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AgreementFeature:
    """One local excursion in the pooled curve, and how every seat testifies
    to it — sign agreement and magnitude agreement, reported separately
    (the campaign's own finding: a feature can agree in sign everywhere and
    still split badly in size, which a single collapsed verdict would hide).
    """

    center_hz: float
    band_hz: tuple[float, float]
    pooled_db: float
    seat_values_db: dict[str, float]
    n_testify: int
    n_dissent: int
    spread_db: float
    ratio: float
    common_mode: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "center_hz": self.center_hz,
            "band_hz": list(self.band_hz),
            "pooled_db": self.pooled_db,
            "seat_values_db": self.seat_values_db,
            "n_testify": self.n_testify,
            "n_dissent": self.n_dissent,
            "spread_db": self.spread_db,
            "ratio": self.ratio,
            "common_mode": self.common_mode,
        }


def _detrend(grid: np.ndarray, curve_db: np.ndarray) -> np.ndarray:
    """Each seat expressed against its own 1-octave moving average.

    A feature is a local excursion, not the gross tilt no narrow biquad can
    or should chase. ``smooth_fractional_octave(..., fraction=1)`` IS a
    1-octave (``2**-0.5`` .. ``2**0.5``) power-mean window, so this is the
    product smoothing seam applied at the campaign tool's own fraction,
    never a hand-rolled second implementation of octave averaging.
    """
    return curve_db - smooth_fractional_octave(grid, curve_db, fraction=1)


def _local_features(
    grid: np.ndarray, pooled: np.ndarray, *, lo_hz: float, hi_hz: float, feature_db: float
) -> list[tuple[int, int, int]]:
    """``(center_idx, lo_idx, hi_idx)`` for every local extremum of
    ``|pooled| >= feature_db`` inside ``[lo_hz, hi_hz]``, half-depth edges."""
    band = (grid >= lo_hz) & (grid <= hi_hz)
    idx = np.where(band)[0]
    found: list[tuple[int, int, int]] = []
    for k in range(1, len(idx) - 1):
        i = idx[k]
        v = pooled[i]
        if abs(v) < feature_db:
            continue
        if v > 0 and not (pooled[i] >= pooled[i - 1] and pooled[i] >= pooled[i + 1]):
            continue
        if v < 0 and not (pooled[i] <= pooled[i - 1] and pooled[i] <= pooled[i + 1]):
            continue
        half = abs(v) / 2.0
        a = i
        while a > idx[0] and abs(pooled[a]) > half and np.sign(pooled[a]) == np.sign(v):
            a -= 1
        b = i
        while b < idx[-1] and abs(pooled[b]) > half and np.sign(pooled[b]) == np.sign(v):
            b += 1
        found.append((i, a, b))
    merged: list[tuple[int, int, int]] = []
    for i, a, b in found:
        if merged and a <= merged[-1][2]:
            prev_i, prev_a, prev_b = merged[-1]
            if abs(pooled[i]) > abs(pooled[prev_i]):
                merged[-1] = (i, min(a, prev_a), max(b, prev_b))
            else:
                merged[-1] = (prev_i, min(a, prev_a), max(b, prev_b))
        else:
            merged.append((i, a, b))
    return merged


def agreement_table(
    seats: Sequence[SeatCurve],
    grid: np.ndarray,
    *,
    lo_hz: float,
    hi_hz: float,
    feature_db: float = 0.4,
    testify_db: float = 0.4,
    magnitude_ratio_ok: float = 3.0,
) -> tuple[AgreementFeature, ...]:
    """Every feature in ``[lo_hz, hi_hz]``, with per-seat testify/dissent
    counts and a magnitude-agreement ratio.

    ``testify`` = same sign as the pooled curve AND ``|seat| >= testify_db``.
    ``dissent`` = opposite sign AND ``|seat| >= testify_db``. ``common_mode``
    requires BOTH sign agreement (``n_testify >= len(seats) - 1`` and
    ``n_dissent <= 1``, ported from the campaign's ``test >= 3 and diss <=
    1`` at 5 seats — generalised to "all but one testify, at most one
    dissents") AND magnitude agreement (``ratio <= magnitude_ratio_ok``).
    """
    grid = np.asarray(grid, dtype=float)
    detrended = np.vstack([_detrend(grid, seat.normalized_db) for seat in seats])
    pooled = detrended.mean(axis=0)
    features = []
    n_seats = len(seats)
    for i, a, b in _local_features(grid, pooled, lo_hz=lo_hz, hi_hz=hi_hz, feature_db=feature_db):
        seat_values = detrended[:, a : b + 1].mean(axis=1)
        p = float(pooled[a : b + 1].mean())
        sign = np.sign(p) if p != 0 else 1.0
        testify = int(np.sum((np.sign(seat_values) == sign) & (np.abs(seat_values) >= testify_db)))
        dissent = int(np.sum((np.sign(seat_values) != sign) & (np.abs(seat_values) >= testify_db)))
        spread = float(seat_values.max() - seat_values.min())
        ratio = float(np.abs(seat_values).max() / max(np.abs(seat_values).min(), 0.01))
        sign_ok = testify >= max(n_seats - 1, 0) and dissent <= 1
        features.append(
            AgreementFeature(
                center_hz=float(grid[i]),
                band_hz=(float(grid[a]), float(grid[b])),
                pooled_db=p,
                seat_values_db={seat.position_id: float(v) for seat, v in zip(seats, seat_values)},
                n_testify=testify,
                n_dissent=dissent,
                spread_db=spread,
                ratio=ratio,
                common_mode=bool(sign_ok and ratio <= magnitude_ratio_ok),
            )
        )
    return tuple(features)
