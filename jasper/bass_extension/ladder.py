# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure, hardware-free bass-extension commissioning state machine (plan §7).

Owns the commissioning *decisions* (state machine, per-rung stop-conditions,
clean ceiling, sustain result, anchor derivation) as pure functions over Wave 1
numerics and the ``MarginPolicy`` thresholds.  Opens no device, socket,
CamillaDSP connection, subprocess, or coordinator; persists nothing; reads no
env var.  ``review -> accepted`` is Wave 3 and intentionally absent — ``accepted``
is not a reachable state.  Neither the injected :func:`synthetic_dry_run` nor
:func:`synthetic_limiter_intake` derives or publishes any ``limiter_threshold_dbfs``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.analysis import (
    THIRD_OCTAVE_BASS_BANDS_HZ,
    band_levels_from_magnitude,
    compression_curve,
    thd_curve,
    tracking_error_db,
)
from jasper.audio_measurement.evidence_identity import ArtifactIdentity
from jasper.bass_extension.adapters.base import (
    CabinetInfo,
    CaptureRole,
    EnclosureAdapter,
    FitRefusal,
    MagnitudeCurve,
    PlantFit,
    TargetSpec,
    adapter_for_enclosure,
)
from jasper.bass_extension.profile import BassExtensionRefusal
from jasper.bass_extension.targets import MARGINS, AnchorPoint, MarginPolicy, interpolate_anchors

# Fixed thresholds not owned by MarginPolicy (plan §7.3/§7.5, provisional).
REPEAT_SPREAD_FAIL_DB = 2.0
THD_EXTENSION_HI_HZ = 80.0
MIC_FIXITY_BAND_HZ = (150.0, 400.0)
MIC_FIXITY_MIN_CORRELATION = 0.98


class LadderState(StrEnum):
    """Commissioning phases (plan §7.2).  ``accepted`` is Wave 3, not here."""

    IDLE = "idle"
    CHARACTERIZE = "characterize"
    FIT = "fit"
    PROPOSE = "propose"
    VERIFY_DEEPEST = "verify_deepest"
    LADDER = "ladder"
    SUSTAIN_TEST = "sustain_test"
    DERIVE_ANCHORS = "derive_anchors"
    REVIEW = "review"
    ABORTED = "aborted"


_ABORT = "abort"
_RESTART = "restart"

# The single explicit forward-transition table.  review has no forward edge
# (review -> accepted enters Wave 3); aborted is terminal.
_FORWARD: Mapping[LadderState, frozenset[LadderState]] = {
    LadderState.IDLE: frozenset({LadderState.CHARACTERIZE}),
    LadderState.CHARACTERIZE: frozenset({LadderState.FIT}),
    LadderState.FIT: frozenset({LadderState.PROPOSE}),
    LadderState.PROPOSE: frozenset({LadderState.VERIFY_DEEPEST}),
    LadderState.VERIFY_DEEPEST: frozenset({LadderState.LADDER}),
    LadderState.LADDER: frozenset({LadderState.SUSTAIN_TEST}),
    LadderState.SUSTAIN_TEST: frozenset({LadderState.DERIVE_ANCHORS}),
    LadderState.DERIVE_ANCHORS: frozenset({LadderState.REVIEW}),
    LadderState.REVIEW: frozenset(),
    LadderState.ABORTED: frozenset(),
}
_TERMINAL = frozenset({LadderState.ABORTED})
# On restart, in-flight measurement work is retired ``interrupted``; idle,
# review (completed, awaiting operator), and aborted survive unchanged.
_RETIRABLE = frozenset(_FORWARD) - {LadderState.IDLE, LadderState.REVIEW, LadderState.ABORTED}
_DATA_FIELDS = frozenset(
    {"captures", "plant_fit", "family", "rungs", "ceiling", "sustain", "anchors", "refusals"}
)


class LadderError(RuntimeError):
    """A pure ladder decision was asked to operate on inconsistent inputs."""


class LadderTransitionError(LadderError):
    """A requested state transition is not in the allowed-transition table."""


class LadderManifestError(ValueError):
    """An injected commissioning manifest is malformed (typed refusal)."""


@dataclass(frozen=True)
class CaptureRecord:
    role: CaptureRole
    identities: tuple[ArtifactIdentity, ...]
    quality_verdict: str


@dataclass(frozen=True)
class RungVerdict:
    passed: bool
    limited_by: str | None = None
    refusal: BassExtensionRefusal | None = None


@dataclass(frozen=True)
class RungMeasurement:
    """Injected per-rung analysis inputs (plan §7.3)."""

    rung_ordinal: int
    commanded_main_volume_db: float
    listening_level: int
    capture_id: ArtifactIdentity
    fund_freqs: Sequence[float]
    fund_db: Sequence[float]
    harmonics: Mapping[int, tuple[Sequence[float], Sequence[float]]]
    predicted_db: Sequence[float]
    decision_band_hz: tuple[float, float]
    noise_floor: tuple[Sequence[float], Sequence[float]] | None = None
    capture_clipped: bool = False
    repeat_spread_db: float = 0.0
    snr_ok: bool = True
    digital_reached: bool = False


@dataclass(frozen=True)
class RungRecord:
    """Retained per-rung evidence summary (plan §7.2)."""

    rung_ordinal: int
    commanded_main_volume_db: float
    listening_level: int
    capture_id: ArtifactIdentity
    band_levels: tuple[float, ...]
    compression_db_by_band: tuple[float, ...]
    thd_summary: float
    tracking_rms_db: float
    tracking_max_db: float
    verdict: RungVerdict


@dataclass(frozen=True)
class CeilingResult:
    listening_level: int | None
    limited_by: str | None
    refusal: BassExtensionRefusal | None
    ceiling_rung_ordinal: int | None


@dataclass(frozen=True)
class SustainResult:
    verdict: str
    fundamental_sag_db: float
    fc_shift_pct: float
    duration_s: float
    limited_by: str | None


@dataclass(frozen=True)
class CommissioningManifest:
    margin: str
    adapter_id: str
    enclosure_kind: str
    cabinet: CabinetInfo
    n_targets: int
    mic_calibration_id: str | None


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    blocking: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class LadderSession:
    """The immutable session snapshot the transition table advances (plan §7.2)."""

    session_id: str
    margin: str
    adapter_id: str
    state: LadderState = LadderState.IDLE
    captures: tuple[CaptureRecord, ...] = ()
    plant_fit: Any = None
    family: tuple[TargetSpec, ...] = ()
    rungs: tuple[RungRecord, ...] = ()
    ceiling: CeilingResult | None = None
    sustain: SustainResult | None = None
    anchors: tuple[AnchorPoint, ...] = ()
    refusals: tuple[str, ...] = ()
    disposition: str | None = None


@dataclass(frozen=True)
class LadderEvent:
    """A transition request: a destination phase, ``abort``, or ``restart``."""

    kind: str
    updates: Mapping[str, Any] | None = None
    reason: str | None = None


def _validated_updates(updates: Mapping[str, Any] | None) -> dict[str, Any]:
    if not updates:
        return {}
    unknown = set(updates) - _DATA_FIELDS
    if unknown:
        raise LadderTransitionError(f"transition may not set fields {sorted(unknown)}")
    return dict(updates)


def transition(session: LadderSession, event: LadderEvent) -> LadderSession:
    """Return the session after one legal transition, else raise.

    Forward events name their destination phase and must be in the allowed table
    for the current state.  ``abort`` moves any non-terminal state to ``aborted``
    and never errors (the red Stop is idempotent).  ``restart`` retires in-flight
    measurement work as ``interrupted``.
    """
    if event.kind == _RESTART:
        if session.state in _RETIRABLE:
            return replace(session, state=LadderState.ABORTED, disposition="interrupted")
        return session
    if event.kind == _ABORT:
        if session.state in _TERMINAL:
            return session
        return replace(session, state=LadderState.ABORTED, disposition=event.reason or "operator_stop")
    try:
        dest = LadderState(event.kind)
    except ValueError as exc:
        raise LadderTransitionError(f"unknown ladder event {event.kind!r}") from exc
    if dest not in _FORWARD[session.state]:
        raise LadderTransitionError(f"transition {session.state.value} -> {dest.value} is not allowed")
    return replace(session, state=dest, **_validated_updates(event.updates))


def start_session(session_id: str, manifest: CommissioningManifest) -> LadderSession:
    """Return a fresh ``idle`` session bound to a validated manifest."""
    return LadderSession(session_id=session_id, margin=manifest.margin, adapter_id=manifest.adapter_id)


def validate_commissioning_manifest(raw: Mapping[str, Any]) -> CommissioningManifest:
    """Strictly validate an injected manifest with no side effects."""
    if not isinstance(raw, Mapping):
        raise LadderManifestError("commissioning manifest must be a mapping")
    unknown = set(raw) - {"margin", "enclosure_kind", "cabinet", "n_targets", "mic_calibration_id"}
    if unknown:
        raise LadderManifestError(f"commissioning manifest has unknown keys {sorted(unknown)}")
    margin = raw.get("margin")
    if not isinstance(margin, str) or margin not in MARGINS:
        raise LadderManifestError("manifest margin must be a known MarginPolicy name")
    enclosure_kind = raw.get("enclosure_kind")
    if not isinstance(enclosure_kind, str) or not enclosure_kind:
        raise LadderManifestError("manifest enclosure_kind must be non-empty text")
    adapter = adapter_for_enclosure(enclosure_kind)
    if adapter is None:
        raise LadderManifestError(BassExtensionRefusal.ENCLOSURE_UNSUPPORTED.value)
    cabinet_raw = raw.get("cabinet")
    if not isinstance(cabinet_raw, Mapping):
        raise LadderManifestError("manifest cabinet must be a mapping")
    try:
        cabinet = CabinetInfo(**cabinet_raw)
    except TypeError as exc:
        raise LadderManifestError(f"manifest cabinet is malformed: {exc}") from exc
    n_targets = raw.get("n_targets", 5)
    if type(n_targets) is not int or n_targets < 2:
        raise LadderManifestError("manifest n_targets must be an integer >= 2")
    mic = raw.get("mic_calibration_id")
    if mic is not None and (not isinstance(mic, str) or not mic):
        raise LadderManifestError("manifest mic_calibration_id must be text or absent")
    return CommissioningManifest(margin, adapter.adapter_id, enclosure_kind, cabinet, n_targets, mic)


def preflight(preconditions: Mapping[str, Any]) -> PreflightResult:
    """Pure, silent precondition gate (plan §7.1) — no logging, no I/O."""
    blocking: list[str] = []
    if not preconditions.get("baseline_applied"):
        blocking.append(BassExtensionRefusal.BASELINE_NOT_APPLIED.value)
    if not preconditions.get("driver_safety_current"):
        blocking.append("driver_safety_not_current")
    if not preconditions.get("measurement_window_available"):
        blocking.append("measurement_window_unavailable")
    if not preconditions.get("capture_reachable"):
        blocking.append("capture_unreachable")
    warnings = () if preconditions.get("mic_calibrated") else ("mic_uncalibrated",)
    return PreflightResult(not blocking, tuple(blocking), warnings)


def dispatch_plant_fit(adapter: EnclosureAdapter, captures: Mapping[CaptureRole, MagnitudeCurve], cabinet: CabinetInfo) -> PlantFit | FitRefusal:
    """Dispatch the fit to the adapter (the fit math lives in Wave 1)."""
    return adapter.fit_plant(captures, cabinet)


def propose_family(adapter: EnclosureAdapter, plant: PlantFit, margin: MarginPolicy, n_targets: int = 5) -> tuple[TargetSpec, ...]:
    """Propose the target family (the family math lives in Wave 1)."""
    return adapter.generate_family(plant, margin=margin, n_targets=n_targets)


def rung_band_levels(freqs: Sequence[float], magnitude_db: Sequence[float]) -> tuple[float, ...]:
    """Third-octave 20–200 Hz band levels for one rung (Wave 1 power-mean)."""
    return band_levels_from_magnitude(
        np.asarray(freqs, dtype=np.float64), np.asarray(magnitude_db, dtype=np.float64), THIRD_OCTAVE_BASS_BANDS_HZ
    )


def _rung_thd_max(m: RungMeasurement) -> float:
    """Max THD ratio below 80 Hz over SNR-valid grid points (band-edge NaN skipped)."""
    _, ratio = thd_curve(
        np.asarray(m.fund_freqs, dtype=np.float64),
        np.asarray(m.fund_db, dtype=np.float64),
        {int(order): curve for order, curve in m.harmonics.items()},
        band=(20.0, THD_EXTENSION_HI_HZ),
        noise_floor=m.noise_floor,
    )
    valid = ratio[np.isfinite(ratio)]
    return float(np.max(valid)) if valid.size else 0.0


def mic_moved(prev_freqs: Sequence[float], prev_db: Sequence[float], cur_freqs: Sequence[float], cur_db: Sequence[float], commanded_delta_db: float) -> bool:
    """True when consecutive gain-normalized rungs decorrelate over 150–400 Hz (plan §7.3)."""
    pf = np.asarray(prev_freqs, dtype=np.float64)
    cf = np.asarray(cur_freqs, dtype=np.float64)
    lo, hi = MIC_FIXITY_BAND_HZ
    grid = pf[(pf >= lo) & (pf <= hi)]
    if grid.size < 3:
        return False
    a = np.interp(grid, pf, np.asarray(prev_db, dtype=np.float64))
    # §7.3 gain-normalization: the commanded step is subtracted for intent/clarity,
    # but the Pearson correlation below is invariant to this additive offset — a
    # mic bump is caught as a broadband *shape* change, not a level change. Kept
    # explicit so the intent survives if the metric ever moves off Pearson.
    b = np.interp(grid, cf, np.asarray(cur_db, dtype=np.float64)) - float(commanded_delta_db)
    if np.std(a) == 0.0 or np.std(b) == 0.0:
        return False
    return float(np.corrcoef(a, b)[0, 1]) < MIC_FIXITY_MIN_CORRELATION


def evaluate_rung(compression_row: Sequence[float], thd_max: float, *, mic_moved_flag: bool, capture_clipped: bool, repeat_spread_db: float, snr_ok: bool, digital_reached: bool, margin: MarginPolicy, decision_band_hz: tuple[float, float], band_edges: tuple[tuple[float, float], ...] = THIRD_OCTAVE_BASS_BANDS_HZ) -> RungVerdict:
    """Apply the plan §7.5 stop-conditions in a fixed precedence.

    Integrity/quality defects (clip, mic-moved, SNR, repeat spread) are checked
    before envelope limits (digital, compression, THD): an untrustworthy capture
    must not be read as clean headroom.
    """
    if capture_clipped:
        return RungVerdict(False, limited_by="mic_clip")
    if mic_moved_flag:
        return RungVerdict(False, refusal=BassExtensionRefusal.MIC_MOVED_BETWEEN_RUNGS)
    if not snr_ok:
        return RungVerdict(False, refusal=BassExtensionRefusal.CAPTURE_SNR_INSUFFICIENT)
    if repeat_spread_db > REPEAT_SPREAD_FAIL_DB:
        return RungVerdict(False, refusal=BassExtensionRefusal.CAPTURE_QUALITY_REFUSED)
    if digital_reached:
        return RungVerdict(False, limited_by="digital")
    lo, hi = decision_band_hz
    for (blo, bhi), value in zip(band_edges, compression_row):
        if lo <= math.sqrt(blo * bhi) <= hi and -float(value) > margin.compression_fail_db:
            return RungVerdict(False, limited_by="compression")
    if thd_max > margin.thd_fail_ratio:
        return RungVerdict(False, limited_by="thd")
    return RungVerdict(True)


def run_ladder(measurements: Sequence[RungMeasurement], margin: MarginPolicy) -> tuple[tuple[RungRecord, ...], CeilingResult]:
    """Evaluate a rung series; stop on the first failure with ceiling = previous rung."""
    if not measurements:
        raise LadderError("a ladder requires at least one rung")
    levels = [rung_band_levels(m.fund_freqs, m.fund_db) for m in measurements]
    series = [(float(m.commanded_main_volume_db), levels[i]) for i, m in enumerate(measurements)]
    compression = compression_curve(series)
    records: list[RungRecord] = []
    ceiling: CeilingResult | None = None
    prev: RungMeasurement | None = None
    for i, m in enumerate(measurements):
        thd_max = _rung_thd_max(m)
        tr_rms, tr_max = tracking_error_db(
            np.asarray(m.fund_freqs, dtype=np.float64), np.asarray(m.fund_db, dtype=np.float64),
            np.asarray(m.predicted_db, dtype=np.float64), m.decision_band_hz,
        )
        moved = prev is not None and mic_moved(
            prev.fund_freqs, prev.fund_db, m.fund_freqs, m.fund_db,
            float(m.commanded_main_volume_db - prev.commanded_main_volume_db),
        )
        verdict = evaluate_rung(
            compression[i], thd_max, mic_moved_flag=moved, capture_clipped=m.capture_clipped,
            repeat_spread_db=m.repeat_spread_db, snr_ok=m.snr_ok, digital_reached=m.digital_reached,
            margin=margin, decision_band_hz=m.decision_band_hz,
        )
        records.append(RungRecord(
            m.rung_ordinal, float(m.commanded_main_volume_db), m.listening_level, m.capture_id,
            levels[i], compression[i], thd_max, tr_rms, tr_max, verdict,
        ))
        if not verdict.passed:
            prev_record = records[i - 1] if i > 0 else None
            ceiling = CeilingResult(
                prev_record.listening_level if prev_record else None, verdict.limited_by,
                verdict.refusal, prev_record.rung_ordinal if prev_record else None,
            )
            break
        prev = m
    if ceiling is None:
        last = records[-1]
        ceiling = CeilingResult(last.listening_level, None, BassExtensionRefusal.LADDER_INCOMPLETE, last.rung_ordinal)
    return tuple(records), ceiling


def sustain_result(pre_band_levels: Sequence[float], post_band_levels: Sequence[float], refit_fc_pre_hz: float, refit_fc_post_hz: float, margin: MarginPolicy) -> SustainResult:
    """Compare a pre/post-hold sweep (plan §7.6); a fail lowers the ceiling one rung."""
    if len(pre_band_levels) != len(post_band_levels) or not pre_band_levels:
        raise LadderError("sustain band-level vectors must be matched and non-empty")
    if refit_fc_pre_hz <= 0.0:
        raise LadderError("sustain pre-hold fit corner must be positive")
    sag = max(float(pre) - float(post) for pre, post in zip(pre_band_levels, post_band_levels))
    fc_shift_pct = abs(float(refit_fc_post_hz) - float(refit_fc_pre_hz)) / float(refit_fc_pre_hz) * 100.0
    limited_by = None
    if sag > margin.sustain_sag_fail_db:
        limited_by = "sustain_sag"
    elif fc_shift_pct > margin.sustain_fc_shift_fail_pct:
        limited_by = "sustain_fc_shift"
    return SustainResult("lowered" if limited_by else "passed", sag, fc_shift_pct, margin.sustain_duration_s, limited_by)


def apply_sustain(rungs: Sequence[RungRecord], ceiling: CeilingResult, sustain: SustainResult) -> CeilingResult:
    """Return the final ceiling: unchanged on pass, one clean rung lower on fail."""
    if sustain.verdict == "passed" or ceiling.ceiling_rung_ordinal is None:
        return ceiling
    lower = [r for r in rungs if r.rung_ordinal < ceiling.ceiling_rung_ordinal and r.verdict.passed]
    if not lower:
        return CeilingResult(None, None, BassExtensionRefusal.LADDER_INCOMPLETE, None)
    p = lower[-1]
    return CeilingResult(p.listening_level, sustain.limited_by, None, p.rung_ordinal)


def derive_anchor_set(family: tuple[TargetSpec, ...], measured: tuple[AnchorPoint, ...], margin: MarginPolicy) -> tuple[AnchorPoint, ...]:
    """Derive one clamped anchor per non-natural target (Wave 1 §7.4 wiring)."""
    return interpolate_anchors(family, measured, margin)


@dataclass(frozen=True)
class DryRunInputs:
    """Every collaborator the synthetic dry run injects (opens nothing real)."""

    manifest: Mapping[str, Any]
    preconditions: Mapping[str, Any]
    adapter: EnclosureAdapter
    capture_curves: Mapping[CaptureRole, MagnitudeCurve]
    capture_records: tuple[CaptureRecord, ...]
    rung_measurements: tuple[RungMeasurement, ...]
    spot_anchors: tuple[AnchorPoint, ...]
    sustain_pre_band_levels: tuple[float, ...]
    sustain_post_band_levels: tuple[float, ...]
    sustain_fc_pre_hz: float
    sustain_fc_post_hz: float
    session_id: str = "bex-dry-run"


def synthetic_dry_run(inputs: DryRunInputs) -> LadderSession:
    """Walk ``idle -> review`` with injected fakes; open no device or I/O (reaches at most review)."""
    manifest = validate_commissioning_manifest(inputs.manifest)
    margin = MARGINS[manifest.margin]
    session = start_session(inputs.session_id, manifest)

    pre = preflight(inputs.preconditions)
    if not pre.ok:
        return replace(transition(session, LadderEvent(_ABORT, reason="preflight_blocked")), refusals=pre.blocking)

    session = transition(session, LadderEvent("characterize", updates={"captures": inputs.capture_records}))
    fit = dispatch_plant_fit(inputs.adapter, inputs.capture_curves, manifest.cabinet)
    if isinstance(fit, FitRefusal):
        return replace(transition(session, LadderEvent(_ABORT, reason="fit_refused")), refusals=(fit.refusal,))
    session = transition(session, LadderEvent("fit", updates={"plant_fit": fit}))

    family = propose_family(inputs.adapter, fit, margin, manifest.n_targets)
    session = transition(session, LadderEvent("propose", updates={"family": family}))
    session = transition(session, LadderEvent("verify_deepest"))

    records, base_ceiling = run_ladder(inputs.rung_measurements, margin)
    session = transition(session, LadderEvent("ladder", updates={"rungs": records, "ceiling": base_ceiling}))

    sustain = sustain_result(
        inputs.sustain_pre_band_levels, inputs.sustain_post_band_levels,
        inputs.sustain_fc_pre_hz, inputs.sustain_fc_post_hz, margin,
    )
    final_ceiling = apply_sustain(records, base_ceiling, sustain)
    session = transition(session, LadderEvent("sustain_test", updates={"sustain": sustain, "ceiling": final_ceiling}))
    if final_ceiling.listening_level is None:
        return replace(transition(session, LadderEvent(_ABORT, reason="ladder_incomplete")), refusals=(BassExtensionRefusal.LADDER_INCOMPLETE,))

    measured = (AnchorPoint(family[0].target_id, final_ceiling.listening_level, "measured"), *inputs.spot_anchors)
    anchors = derive_anchor_set(family, measured, margin)
    session = transition(session, LadderEvent("derive_anchors", updates={"anchors": anchors}))
    return transition(session, LadderEvent("review"))


def synthetic_limiter_intake(evidence: object, required_context: object):
    """Pass injected evidence through the frozen limiter producer in memory.

    Consumes the typed ``LimiterThresholdSet`` / ``LimiterEvidenceRefusal`` and
    establishes no real limiter value.  The producer is imported *function-local*
    so no module-scope (production) import path can reach it.
    """
    from jasper.bass_extension.limiter_evidence import produce_limiter_thresholds

    return produce_limiter_thresholds(evidence, required_context=required_context)
