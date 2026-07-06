# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Measurement-session state machine.

Phase 2: multi-position MMM averaging + verify pass.

The session owns the measurement state machine. The web handler opens
a fresh measurement window for each sweep so renderers pause only while
the speaker is actively measuring. Browser-side calls arm capture,
upload pre-sweep room noise, trigger sweep playback, upload the captured
response, and eventually apply or verify the generated correction.

State transitions in the browser flow (with N = total_positions):

    IDLE → NEEDS_NOISE_CAPTURE → PREPARING → SWEEPING → AWAITING_CAPTURE
         → (on_capture_uploaded for position 0 ... N-2)
         → optional NEEDS_REPEAT_CAPTURE → AWAITING_REPEAT_CAPTURE
         → NEEDS_NEXT_POSITION
         → NEEDS_NOISE_CAPTURE → SWEEPING (position 1)
         → ...
         → AWAITING_CAPTURE (position N-1)
         → on_capture_uploaded for last position
         → ANALYZING (spatial avg + PEQ design)
         → READY → APPLIED
         → VERIFYING (separate fresh window) → AWAITING_VERIFY_CAPTURE
         → VERIFIED  (back to APPLIED with verify_curve populated)

Single-position is N=1: the same flow, just no NEEDS_NEXT_POSITION.

Phase 1 callers (the old `prepare_and_play_sweep` API) still work —
it's a thin wrapper that opens a fresh window for one position
and closes it when done.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np

from jasper.audio_measurement import (
    analysis,
    calibration,
    deconv,
    quality,
    sweep,
)
from jasper.audio_measurement.calibration import CalibrationRecord
from jasper.audio_measurement.quality_model import ROOM as ROOM_QUALITY
from jasper.audio_measurement.ramp import RECOVERABLE_ERRORS

from . import (
    acceptance,
    acoustic_quality,
    browser_audio,
    confidence,
    runtime_integrity,
    strategy,
)
from .artifacts import ANALYSIS_NORMALIZE_BAND_HZ, SessionArtifacts
from .autolevel import (
    AutolevelController,
    AutolevelData as AutolevelData,
    AutolevelStatus as AutolevelStatus,
    compute_autolevel_cap as compute_autolevel_cap,
)
from .level_match import (
    LevelLockStore,
    LevelMatchOutcome,
    LevelMatchSession,
    MicGeometry as MicGeometry,
)
from .peq import PEQ
from .state_guard import SessionStateGuard
from .status import (
    describe_current_config as describe_current_config,
    parse_current_correction as parse_current_correction,
    session_snapshot,
)
from ..log_event import log_event

logger = logging.getLogger(__name__)


def _bundles_enabled() -> bool:
    """Default ON; opt-out via JASPER_CORRECTION_SAVE_BUNDLES=0."""
    return os.environ.get("JASPER_CORRECTION_SAVE_BUNDLES", "1").strip() != "0"


DBFS_FLOOR = -120.0
SNR_BANDS_HZ: tuple[tuple[str, float, float], ...] = (
    ("sub_bass", 20.0, 80.0),
    ("bass", 80.0, 160.0),
    ("upper_bass", 160.0, 350.0),
    ("transition", 350.0, 1000.0),
)


def _dbfs(value: float) -> float:
    if value <= 0 or not np.isfinite(value):
        return DBFS_FLOOR
    return max(DBFS_FLOOR, 20.0 * math.log10(value))


def _band_levels_dbfs(samples: np.ndarray, sample_rate: int) -> list[dict[str, Any]]:
    if samples.ndim != 1 or sample_rate <= 0 or samples.size < 8:
        return []
    # Bound the FFT input the same way deconvolve() does: callers pass
    # uploaded WAVs (noise floor, capture band-SNR) limited only by the
    # 32 MB HTTP body cap, so an oversized/stuck upload would otherwise
    # drive this rfft + hanning to OOM on the 1 GB Pi. Band levels need
    # only a few seconds; sweep_len=0 = "nothing to preserve".
    samples = deconv.cap_capture_length(samples, sweep_len=0, sample_rate=sample_rate)
    x = np.asarray(samples, dtype=np.float64)
    window = np.hanning(x.size)
    spectrum = np.fft.rfft(x * window)
    freqs = np.fft.rfftfreq(x.size, d=1.0 / sample_rate)
    power = np.abs(spectrum) ** 2
    out: list[dict[str, Any]] = []
    for band_id, low, high in SNR_BANDS_HZ:
        mask = (freqs >= low) & (freqs < high)
        if not np.any(mask):
            continue
        rms_like = math.sqrt(float(np.mean(power[mask]))) / max(1, x.size)
        out.append({
            "band_id": band_id,
            "band_hz": [low, high],
            "level_dbfs": round(_dbfs(rms_like), 2),
        })
    return out


class SessionState(Enum):
    IDLE = "idle"
    NEEDS_NOISE_CAPTURE = "needs_noise_capture"
    PREPARING = "preparing"
    SWEEPING = "sweeping"
    AWAITING_CAPTURE = "awaiting_capture"
    NEEDS_REPEAT_CAPTURE = "needs_repeat_capture"
    AWAITING_REPEAT_CAPTURE = "awaiting_repeat_capture"
    NEEDS_NEXT_POSITION = "needs_next_position"
    ANALYZING = "analyzing"
    READY = "ready"
    APPLIED = "applied"
    VERIFYING = "verifying"
    AWAITING_VERIFY_CAPTURE = "awaiting_verify_capture"
    VERIFIED = "verified"
    FAILED = "failed"


class SessionBusyError(RuntimeError):
    """An operation was refused because a transient sweep/analysis task is
    running and would race it. Distinct from a generic error so the web
    layer can map it to HTTP 409 (Conflict) rather than 500, and so the
    autolevel volume-restore can tell "rejected, measurement still live"
    apart from "reset completed". Subclasses RuntimeError for back-compat
    with callers that catch the broader type."""


@dataclass
class CurveJSON:
    freqs_hz: list[float]
    magnitude_db: list[float]


@dataclass
class PEQJSON:
    freq_hz: float
    q: float
    gain_db: float

    @classmethod
    def from_peq(cls, p: PEQ) -> "PEQJSON":
        return cls(freq_hz=p.freq, q=p.q, gain_db=p.gain)


@dataclass
class SessionEvent:
    seq: int
    timestamp: float
    type: str
    payload: dict[str, Any]


@dataclass
class SessionConfig:
    sweep_dir: Path = Path("/var/lib/jasper/correction/sweeps")
    capture_dir: Path = Path("/var/lib/jasper/correction/captures")
    sessions_dir: Path = Path("/var/lib/jasper/correction/sessions")
    config_dir: Path = Path("/var/lib/camilladsp/configs")
    base_config_path: Path = Path("/etc/camilladsp/outputd-cutover.yml")
    calibration_dir: Path = calibration.DEFAULT_CALIBRATION_DIR

    f1_hz: float = 20.0
    f2_hz: float = 20000.0
    duration_s: float = 10.0
    sample_rate: int = 48000
    amplitude_dbfs: float = -12.0

    peq_f_low: float = 20.0
    peq_f_high: float = 350.0
    peq_max_filters: int = 5
    peq_max_cut_db: float = -10.0
    peq_max_boost_db: float = 3.0
    peq_cuts_only: bool = True
    peq_flatness_target_db: float = 1.0
    correction_strategy: str = strategy.DEFAULT_CORRECTION_STRATEGY_ID


# After a sweep the session waits in an awaiting_*_capture state for the
# browser to upload the recording. If that upload never arrives (iOS screen
# lock, backgrounded tab, network blip), the session would wedge forever and
# block every future /start. This watchdog abandons such a stranded session so
# the wizard self-recovers. A sweep+upload normally completes in seconds; 120 s
# is generous headroom. Mirrors voice_daemon's measurement-window auto-clear.
AWAITING_CAPTURE_TIMEOUT_SEC = 120.0

# States the stranded-capture watchdog guards: every state where the session
# is parked waiting on the BROWSER to upload audio it records automatically,
# with no user action in the loop. If that upload never arrives — denied mic
# permission, a backgrounded iOS tab, a closed page — the session would wedge
# forever and block every future /start. needs_noise_capture is included
# because it is exactly that shape: an automatic pre-sweep noise recording,
# entered before any measurement window opens (so a wedge there never leaves
# the speaker muted — see begin_noise_capture / prepare_and_play_sweep). The
# user-paced needs_next_position / needs_repeat_capture states are deliberately
# NOT guarded: the user may legitimately take minutes to reposition the phone,
# and those states already carry their own Cancel affordance.
_CAPTURE_TIMEOUT_STATES = frozenset({
    SessionState.NEEDS_NOISE_CAPTURE,
    SessionState.AWAITING_CAPTURE,
    SessionState.AWAITING_REPEAT_CAPTURE,
    SessionState.AWAITING_VERIFY_CAPTURE,
})

# States reset() refuses, because a fire-and-forget sweep/analysis task is
# actively running and will set the next state AFTER reset() sets IDLE —
# leaving the session looking reset for an instant and then jumping back.
# These are exactly the states the wizard never offers Cancel / Reset from
# (see cancellableStates in correction/js/main.js), so rejecting them here
# breaks no UI affordance; it only fences a stale/buggy client off the race.
# Every settled, parked, or wedged state (idle / needs_* / awaiting_* / ready
# / applied / verified / failed) still resets, so the escape hatch keeps
# working when the user actually needs it.
_RESET_BUSY_STATES = frozenset({
    SessionState.PREPARING,
    SessionState.SWEEPING,
    SessionState.ANALYZING,
    SessionState.VERIFYING,
})


class MeasurementSession:
    """Multi-position measurement session.

    The HTTP handler creates one MeasurementSession per `POST /start`
    call. It lives until the next `/start` (which replaces it) or
    until the daemon restarts. State + curves persist as long as
    the session does.
    """

    def __init__(
        self,
        cfg: SessionConfig | None = None,
        *,
        total_positions: int = 1,
        target_choice: str = "flat",
        strategy_choice: str | None = None,
        mic_calibration: CalibrationRecord | None = None,
        input_device: dict[str, Any] | None = None,
        repeat_main_position: bool = False,
    ) -> None:
        self.cfg = cfg or SessionConfig()
        self.session_id = uuid.uuid4().hex[:12]
        self.state = SessionState.IDLE
        self.started_at = time.time()
        self.updated_at = self.started_at
        self.error: str | None = None

        self.total_positions = max(1, int(total_positions))
        self.current_position = 0
        self.target_choice = strategy.resolve_target_profile(
            target_choice,
        ).target_id
        self.strategy_choice = strategy.resolve_correction_strategy(
            strategy_choice or self.cfg.correction_strategy,
        ).strategy_id
        self.mic_calibration = mic_calibration
        self.input_device = input_device
        self.repeat_main_position = bool(repeat_main_position)
        self.browser_audio_report = browser_audio.assess_browser_audio_path(
            input_device=input_device,
            expected_sample_rate=self.cfg.sample_rate,
            has_mic_calibration=mic_calibration is not None,
        ).to_dict()
        # Per-position smoothed magnitude responses (dB on log grid).
        # Spatial-averaged at end of multi-position flow.
        self.position_magnitudes: list[np.ndarray] = []
        self.position_freqs: np.ndarray | None = None  # log grid
        self.capture_quality: list[dict[str, Any]] = []
        self.noise_reports: list[dict[str, Any]] = []
        self.repeat_quality: dict[str, Any] | None = None
        self.repeat_curve: CurveJSON | None = None
        self.repeatability_report: dict[str, Any] | None = None
        self.verify_quality: dict[str, Any] | None = None
        self.confidence_report: dict[str, Any] | None = None
        self.acoustic_quality: dict[str, Any] | None = None
        self.runtime_integrity = runtime_integrity.RuntimeIntegrityReport(
            self.session_id,
        )
        self.position_analysis: dict[str, Any] | None = None

        # Output curves for the chart.
        self.measured_curve: CurveJSON | None = None
        self.target_curve: CurveJSON | None = None
        self.predicted_curve: CurveJSON | None = None
        # Pre-correction curve at position 1 (the FIRST measured position),
        # retained as the matched comparison basis for the P4 acceptance
        # verdict: the verify capture is taken at position 1, so comparing it
        # against this same-geometry curve is apples-to-apples (the spatial
        # average mixes several seats — see acceptance.py). None for a session
        # that never captured a position; the evaluator falls back to
        # measured_curve then.
        self.position1_curve: CurveJSON | None = None
        self.verify_curve: CurveJSON | None = None
        self.verify_metrics: dict[str, float] | None = None
        # Honest MEASURED before/after readout, populated by the verify
        # path: pre-correction measured deviation, post-correction
        # verify deviation (both over the SAME band as verify_metrics),
        # the measured delta, and fill_segments for the browser. None
        # until a verify measurement lands — the predicted design figure
        # is never promoted into "improvement" without this.
        self.verify_before_after: dict[str, Any] | None = None
        # P4 deterministic acceptance verdict (accept / surface /
        # revert_pending_confirm / revert), populated by on_verify_capture_-
        # uploaded from the pure AcceptanceEvaluator. None until a verify
        # lands. `_verify_count` and `_prior_clear_regression` track the
        # confirmatory-re-measure concordance across re-verifies: a clear
        # regression auto-reverts only when the verify IMMEDIATELY AFTER it
        # concurs (strict adjacency — a clean verify answers the pending
        # question and clears the flag).
        self.acceptance: dict[str, Any] | None = None
        self._verify_count = 0
        self._prior_clear_regression = False
        # Outcome of the automatic rollback, recorded when it actually
        # completes (never predicted): {"result": "ok"|"failed", "at": ts}.
        # None = no auto-revert has run to completion (not attempted, or still
        # in flight after an upload-response timeout — the coroutine keeps
        # running and records the truth when reset() finishes, which is why
        # this lives on the session rather than in the HTTP response). The
        # envelope reads it to tell the household the truth post-revert: a
        # successful revert lands the session in IDLE, where the honest
        # "reverted" copy is driven by this field; a failed one leaves the
        # correction APPLIED and the result-screen copy must say so.
        self.auto_revert_outcome: dict[str, Any] | None = None
        self.design_report: dict[str, Any] | None = None

        self.peqs: list[PEQJSON] = []
        self.config_path: Path | None = None
        # The CamillaDSP config path that was live immediately BEFORE apply()
        # swapped in this correction. Captured at apply time so a confirmed-
        # regression auto-revert can restore exactly the prior graph through
        # the existing reset() path (never a new reversal mechanism). None
        # until apply() runs with a config getter.
        self.pre_apply_config_path: str | None = None
        self.pre_measurement_config_path: Path | None = None
        self.measurement_config_path: Path | None = None

        # Sweep cache.
        self.sweep_meta: sweep.SweepMeta | None = None
        self.sweep_wav_path: Path | None = None
        self.last_capture_path: Path | None = None

        # Auto-level is orthogonal to the measurement state machine. The
        # controller owns the ramp events and retained main_volume setter; the
        # session keeps the public methods for web-handler compatibility.
        self._autolevel_controller = AutolevelController(
            session_id=self.session_id,
        )

        # P2 relay-closed level match. The settle-based RampController (shared
        # kernel) is the generalization of AutolevelController; the browser-locked
        # AutolevelController above stays as the no-relay/local fallback. The lock
        # store is per-geometry (near-field baffle vs listening position), so a
        # near-field lock and a listening-position lock coexist. `run_level_match`
        # drives it; the store's drift reference is checked on later sweeps.
        self.level_lock_store = LevelLockStore()
        self._last_level_match: LevelMatchOutcome | None = None
        # Single-flight slot for the CURRENT run's LevelMatchSession, so a
        # manual Lock / Cancel from the flow can reach the running
        # RampController through `lock_level_match` / `cancel_level_match`. A
        # bare local (the prior shape) discarded the controller the instant
        # `run_level_match` awaited, so neither seam could ever fire. NOTE this
        # is a per-run SLOT, unlike `_autolevel_controller` (a permanent
        # controller object): `run_level_match` refuses to start while it is
        # occupied and clears it identity-guarded, so an overlapping run can
        # never orphan a live ramp from its Cancel seam.
        self._level_match_session: LevelMatchSession | None = None

        # Single-slot guard that abandons stranded browser-capture states and
        # refuses reset while a fire-and-forget sweep/analysis task is active.
        # Armed/cancelled centrally from _set_state; capture_timeout_sec remains
        # an overridable session property for tests (and disables when <= 0).
        self._state_guard = SessionStateGuard(
            session_id=self.session_id,
            capture_timeout_states=_CAPTURE_TIMEOUT_STATES,
            reset_busy_states=_RESET_BUSY_STATES,
            capture_timeout_sec=AWAITING_CAPTURE_TIMEOUT_SEC,
            get_state=lambda: self.state,
            lock_factory=lambda: self._lock,
            fail=self._fail,
            state_label=lambda state: state.value,
            logger=logger,
        )

        # Optional client-reported room noise floor (the autolevel
        # preflight measures this in the browser before the tone
        # plays). Saved into info.json so debug bundles preserve the
        # context that drove the autolevel target band.
        self.noise_floor_db: float | None = None

        # Snapshot of the active CamillaDSP config at the moment
        # `/start` was hit, BEFORE the auto-reset to base config. Lets
        # the bundle reproduce what state the speaker was in when this
        # session began, including custom configs that JTS cannot
        # safely preserve.
        self.current_correction_at_start: dict[str, Any] | None = None

        # Per-session debug bundle. All artifacts (info.json,
        # result.json, per-position WAVs, verify.wav, applied.yml)
        # and mic_calibration.* land here. The directory is created
        # lazily on first write so tests that pass a SessionConfig
        # pointing at a tmp_path don't have to pre-mkdir.
        self.bundle_dir: Path = self.cfg.sessions_dir / self.session_id
        self.save_bundles: bool = _bundles_enabled()
        self.artifacts = SessionArtifacts(self)

        # Events retained for debugging / future progress streams. The
        # shipped browser UI currently polls GET /status.
        self._events: list[SessionEvent] = []
        self._event_seq = 0
        # Lazy-init the asyncio.Lock — Python 3.9 binds it to the
        # current loop at construction time and raises if none is
        # running. Construction happens from sync HTTP-handler
        # threads, so deferring until first async use keeps the
        # session safe to instantiate anywhere.
        self._lock_obj: asyncio.Lock | None = None

    @property
    def _lock(self) -> asyncio.Lock:
        if self._lock_obj is None:
            self._lock_obj = asyncio.Lock()
        return self._lock_obj

    @property
    def autolevel(self) -> AutolevelData:
        return self._autolevel_controller.data

    @autolevel.setter
    def autolevel(self, data: AutolevelData) -> None:
        self._autolevel_controller.data = data

    @property
    def _main_volume_setter(
        self,
    ) -> Callable[[float], Awaitable[Any]] | None:
        return self._autolevel_controller.main_volume_setter

    @_main_volume_setter.setter
    def _main_volume_setter(
        self,
        setter: Callable[[float], Awaitable[Any]] | None,
    ) -> None:
        self._autolevel_controller.main_volume_setter = setter

    @property
    def capture_timeout_sec(self) -> float:
        return self._state_guard.capture_timeout_sec

    @capture_timeout_sec.setter
    def capture_timeout_sec(self, timeout_sec: float) -> None:
        self._state_guard.capture_timeout_sec = float(timeout_sec)

    # ------------------------------------------------------------------
    # Internal helpers.
    # ------------------------------------------------------------------

    def _emit(self, type_: str, payload: dict[str, Any]) -> None:
        self._event_seq += 1
        ev = SessionEvent(
            seq=self._event_seq,
            timestamp=time.time(),
            type=type_,
            payload=payload,
        )
        self._events.append(ev)
        self.updated_at = ev.timestamp

    def _cancel_capture_timeout(self) -> None:
        self._state_guard.cancel_capture_timeout()

    async def _set_state(self, state: SessionState, **extra: Any) -> None:
        prev = self.state
        self.state = state
        # Re-arm the stranded-capture watchdog on every transition: cancel any
        # pending timer, then start a fresh one only when entering a state that
        # waits on an automatic browser upload. An upload (or any other
        # transition) cancels it for free.
        self._state_guard.on_transition(state)
        payload = {"state": state.value, "prev": prev.value, **extra}
        self._emit("state", payload)
        logger.info(
            "session %s: %s → %s %s",
            self.session_id, prev.value, state.value,
            extra if extra else "",
        )
        # Bundle artifacts are best-effort — never let a write failure
        # bring down a measurement state transition.
        try:
            self._write_info_json()
        except Exception:  # noqa: BLE001
            logger.exception(
                "bundle info.json write failed (state=%s)", state.value,
            )

    # ------------------------------------------------------------------
    # Bundle artifacts. MeasurementSession keeps this public/internal surface
    # for callers, while SessionArtifacts owns the file-system work.
    # ------------------------------------------------------------------

    def _ensure_bundle_dir(self) -> Path | None:
        return self.artifacts.ensure_bundle_dir()

    def _bundle_relative_path(self, path: Path) -> str | None:
        return self.artifacts.bundle_relative_path(path)

    def _existing_bundle_dependencies(self, *paths: str) -> list[str]:
        return self.artifacts.existing_bundle_dependencies(*paths)

    def _write_capture_replay_artifacts(
        self,
        captured_wav_path: Path,
        *,
        capture_kind: str,
        position_index: int | None,
        ir: np.ndarray,
        raw_freqs_hz: np.ndarray,
        raw_magnitude_db: np.ndarray,
        smoothed_magnitude_db: np.ndarray,
        log_freqs_hz: np.ndarray,
        log_magnitude_db: np.ndarray,
        direct_arrival: dict[str, Any],
    ) -> dict[str, Any] | None:
        return self.artifacts.write_capture_replay_artifacts(
            captured_wav_path,
            capture_kind=capture_kind,
            position_index=position_index,
            ir=ir,
            raw_freqs_hz=raw_freqs_hz,
            raw_magnitude_db=raw_magnitude_db,
            smoothed_magnitude_db=smoothed_magnitude_db,
            log_freqs_hz=log_freqs_hz,
            log_magnitude_db=log_magnitude_db,
            direct_arrival=direct_arrival,
        )

    def _record_raw_capture_artifact(
        self,
        captured_wav_path: Path,
        *,
        capture_kind: str,
        position_index: int | None = None,
    ) -> None:
        self.artifacts.record_raw_capture_artifact(
            captured_wav_path,
            capture_kind=capture_kind,
            position_index=position_index,
        )

    def _refresh_acoustic_quality(self) -> None:
        self.acoustic_quality = acoustic_quality.build_acoustic_quality_report(
            session_id=self.session_id,
            capture_quality=self.capture_quality,
            noise_reports=self.noise_reports,
            repeat_quality=self.repeat_quality,
            repeatability=self.repeatability_report,
            verify_quality=self.verify_quality,
        )

    def _write_acoustic_quality_json(self) -> None:
        self.artifacts.write_acoustic_quality_json()

    def _write_runtime_integrity_json(
        self,
        *,
        extra_dependencies: tuple[str, ...] = (),
    ) -> None:
        self.artifacts.write_runtime_integrity_json(
            extra_dependencies=extra_dependencies,
        )

    def _log_runtime_integrity_issues(
        self,
        issues: list[dict[str, Any]],
    ) -> None:
        for issue in issues:
            log_event(
                logger,
                "correction_runtime_integrity_issue",
                session=self.session_id,
                code=issue.get("code"),
                severity=issue.get("severity"),
                capture_kind=issue.get("capture_kind"),
                position_index=issue.get("position_index"),
                message=issue.get("message"),
                level=logging.WARNING,
            )

    async def _record_runtime_snapshot(
        self,
        label: str,
        *,
        capture_kind: str | None,
        position_index: int | None,
        runtime_probe_async: Callable[[], Awaitable[dict[str, Any] | None]] | None,
    ) -> None:
        camilla_status = None
        if runtime_probe_async is not None:
            try:
                camilla_status = await runtime_probe_async()
            except Exception as e:  # noqa: BLE001
                log_event(
                    logger,
                    "correction_runtime_probe_failed",
                    session=self.session_id,
                    label=label,
                    error=e,
                    level=logging.DEBUG,
                )
        issues = self.runtime_integrity.record_snapshot(
            label,
            capture_kind=capture_kind,
            position_index=position_index,
            camilla_status=camilla_status,
        )
        self._log_runtime_integrity_issues(issues)
        try:
            self._write_runtime_integrity_json()
        except Exception:  # noqa: BLE001
            logger.exception("bundle runtime_integrity.json write failed")

    def _record_runtime_capture(
        self,
        captured_wav_path: Path,
        *,
        capture_kind: str,
        position_index: int | None,
    ) -> None:
        if self.sweep_meta is None:
            return
        rel_path = self._bundle_relative_path(captured_wav_path)
        issues = self.runtime_integrity.record_capture(
            captured_wav_path,
            capture_kind=capture_kind,
            position_index=position_index,
            artifact_path=rel_path,
            expected_sample_rate=self.cfg.sample_rate,
            expected_sweep_samples=self.sweep_meta.n_samples,
            expected_sweep_duration_s=self.sweep_meta.duration_s,
        )
        self._log_runtime_integrity_issues(issues)
        try:
            self._write_runtime_integrity_json(
                extra_dependencies=(rel_path,) if rel_path else (),
            )
        except Exception:  # noqa: BLE001
            logger.exception("bundle runtime_integrity.json write failed")

    def capture_path_for_position(self, idx: int) -> Path:
        """Where a per-position WAV should be written. Falls back to
        cfg.capture_dir when bundles are disabled or the per-session
        dir can't be created — keeps the upload path working even
        when /var/lib/jasper is read-only or full."""
        return self.artifacts.capture_path_for_position(idx)

    def noise_capture_path_for_position(self, idx: int) -> Path:
        """Where the pre-sweep noise WAV for a position should land."""
        return self.artifacts.noise_capture_path_for_position(idx)

    def repeat_capture_path_for_position(
        self,
        idx: int = 0,
        *,
        repeat_index: int = 1,
    ) -> Path:
        """Where optional same-position repeat WAVs should land."""
        return self.artifacts.repeat_capture_path_for_position(
            idx,
            repeat_index=repeat_index,
        )

    def verify_capture_path(self) -> Path:
        """Where the post-Apply re-measurement WAV should land."""
        return self.artifacts.verify_capture_path()

    def _write_info_json(self) -> None:
        """Atomically rewrite info.json with the current session snapshot."""
        self.artifacts.write_info_json()

    def _write_result_json(self) -> None:
        """Snapshot the chart curves + verify after design / verify."""
        self.artifacts.write_result_json()

    def _write_mic_calibration_bundle(self, bundle: Path) -> None:
        self.artifacts.write_mic_calibration_bundle(bundle)

    def _write_position_analysis_json(self) -> None:
        """Persist replayable per-position curves and variance bands."""
        self.artifacts.write_position_analysis_json()

    def _copy_applied_yaml(self) -> None:
        """Copy the just-emitted correction YAML into the bundle."""
        self.artifacts.copy_applied_yaml()

    async def state_changed_from(
        self,
        from_states: SessionState | set[SessionState],
        *,
        timeout_s: float = 5.0,
    ) -> bool:
        """Block until session state is no longer in `from_states`.

        Used by HTTP handlers that kick off background async tasks
        and want to return the *new* state to the client — without
        this wait, the client briefly sees stale pre-transition
        state and (in the case of pollState's needs_next_position /
        applied / verified branches) STOPS POLLING, missing all
        subsequent state changes. The bug surfaced as
        `cannot advance to next position from state awaiting_capture`
        when the user double-tapped Continue after the polling died
        silently.

        Returns True if state changed, False on timeout.
        """
        if isinstance(from_states, SessionState):
            from_states = {from_states}
        else:
            from_states = set(from_states)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            if self.state not in from_states:
                return True
            await asyncio.sleep(0.02)
        return False

    async def _fail(self, message: str) -> None:
        self._cancel_capture_timeout()
        self.error = message
        self.state = SessionState.FAILED
        self._emit("error", {"message": message})
        logger.error("session %s failed: %s", self.session_id, message)
        try:
            self._write_info_json()
        except Exception:  # noqa: BLE001
            logger.exception(
                "bundle info.json write failed (state=%s)", self.state.value,
            )
        # A failed measurement must not strand the speaker at the loud
        # autolevel level — the web apply/reset handlers, which normally
        # restore it, never run on this path (watchdog timeout, analysis
        # error, etc.).
        await self._restore_listening_volume_if_ramped()

    async def _restore_listening_volume_if_ramped(self) -> None:
        """Restore main_volume to the pre-autolevel listening level when a
        measurement ends on a path the web apply/reset handlers don't cover
        (watchdog FAILED, verify VERIFIED).

        Autolevel ramps main_volume up to a measurement level and leaves it
        LOCKED for the whole measurement (run_autolevel never resets
        session.state), so without this hook a failed or verify-ended
        measurement would leave the speaker loud until the next /reset.
        Best-effort and idempotent — the apply/reset HTTP handlers own the
        success paths; this fires only for the endings they never see. It
        holds no lock and swallows errors, so it is safe to call from _fail
        (which runs under the session lock)."""
        await self._autolevel_controller.restore_listening_volume_if_ramped()

    def _ensure_sweep_cache(self) -> tuple[Path, sweep.SweepMeta]:
        """Generate or reuse the cached sweep WAV. Cached on disk
        because the sweep is deterministic per parameter tuple — no
        point regenerating each measurement (saves ~50 ms per
        position)."""
        self.cfg.sweep_dir.mkdir(parents=True, exist_ok=True)
        sweep_path = self.cfg.sweep_dir / (
            f"sweep_{int(self.cfg.f1_hz)}_{int(self.cfg.f2_hz)}_"
            f"{int(self.cfg.duration_s * 1000)}ms_"
            f"{self.cfg.sample_rate}Hz_"
            f"{int(abs(self.cfg.amplitude_dbfs) * 10)}dbm.wav"
        )
        signal, meta = sweep.synchronized_swept_sine(
            f1=self.cfg.f1_hz,
            f2=self.cfg.f2_hz,
            duration_approx_s=self.cfg.duration_s,
            sample_rate=self.cfg.sample_rate,
            amplitude_dbfs=self.cfg.amplitude_dbfs,
        )
        if not sweep_path.exists():
            sweep.write_sweep_wav(sweep_path, signal, self.cfg.sample_rate)
        self.sweep_wav_path = sweep_path
        self.sweep_meta = meta
        return sweep_path, meta

    def _noise_report_dict(
        self,
        noise_wav_path: Path,
        *,
        position_index: int,
    ) -> dict[str, Any]:
        samples, sample_rate = sweep.read_wav_mono(noise_wav_path)
        # Bound the noise array up front: the /upload-noise body is limited
        # only by the 32 MB HTTP cap, and the rms/peak/abs math below (plus
        # _band_levels_dbfs) would otherwise spike memory on the 1 GB Pi.
        samples = deconv.cap_capture_length(samples, sweep_len=0, sample_rate=sample_rate)
        samples64 = samples.astype(np.float64)
        abs_samples = np.abs(samples64)
        rms = (
            float(np.sqrt(np.mean(samples64 ** 2)))
            if samples64.size
            else 0.0
        )
        peak = float(np.max(abs_samples)) if abs_samples.size else 0.0
        artifact_path: Path | str = noise_wav_path
        if self.bundle_dir is not None:
            try:
                artifact_path = noise_wav_path.relative_to(self.bundle_dir)
            except ValueError:
                pass
        return {
            "capture_kind": "noise",
            "position_index": position_index,
            "artifact_path": str(artifact_path),
            "sample_rate": int(sample_rate),
            "duration_s": round(
                float(samples64.size / sample_rate) if sample_rate > 0 else 0.0,
                3,
            ),
            "rms_dbfs": round(_dbfs(rms), 2),
            "peak_dbfs": round(_dbfs(peak), 2),
            "band_noise_dbfs": _band_levels_dbfs(samples64, sample_rate),
            "method": "pre_sweep_silence_wav",
        }

    def _noise_report_for_position(
        self,
        position_index: int | None,
    ) -> dict[str, Any] | None:
        if position_index is None:
            return None
        for report in reversed(self.noise_reports):
            if report.get("position_index") == position_index:
                return report
        return None

    def _capture_band_snr(
        self,
        captured_wav_path: Path,
        noise_report: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        if not noise_report:
            return []
        try:
            captured, sample_rate = sweep.read_wav_mono(captured_wav_path)
        except Exception:  # noqa: BLE001
            return []
        # Bound before the float64 cast (mirrors _noise_report_dict): this
        # re-reads the raw upload from disk, so an oversized capture would
        # otherwise pay a full-length 64-bit copy before the FFT cap fires.
        captured = deconv.cap_capture_length(captured, sweep_len=0, sample_rate=sample_rate)
        capture_levels = _band_levels_dbfs(captured.astype(np.float64), sample_rate)
        noise_by_band = {
            band.get("band_id"): band
            for band in noise_report.get("band_noise_dbfs") or []
            if isinstance(band, dict)
        }
        out: list[dict[str, Any]] = []
        for capture_band in capture_levels:
            band_id = capture_band.get("band_id")
            noise_band = noise_by_band.get(band_id)
            if not noise_band:
                continue
            capture_db = float(capture_band["level_dbfs"])
            noise_db = float(noise_band["level_dbfs"])
            out.append({
                "band_id": band_id,
                "band_hz": capture_band.get("band_hz"),
                "capture_level_dbfs": round(capture_db, 2),
                "noise_level_dbfs": round(noise_db, 2),
                "estimated_snr_db": round(capture_db - noise_db, 2),
                "method": "fft_band_power_difference",
            })
        return out

    def _direct_arrival_report(self, impulse_response: np.ndarray) -> dict[str, Any]:
        ir = np.asarray(impulse_response, dtype=np.float64)
        if ir.ndim != 1 or ir.size < 8:
            return {"available": False, "reason": "impulse response unavailable"}
        peak_index = int(np.argmax(np.abs(ir)))
        pre_end = max(0, peak_index - int(0.002 * self.cfg.sample_rate))
        pre_start = max(0, pre_end - int(0.02 * self.cfg.sample_rate))
        pre = ir[pre_start:pre_end]
        if pre.size < 8:
            return {
                "available": False,
                "reason": "not enough pre-arrival samples before direct peak",
                "direct_peak_index": peak_index,
            }
        floor_rms = float(np.sqrt(np.mean(pre ** 2)))
        direct_peak = float(np.max(np.abs(ir)))
        return {
            "available": True,
            "direct_peak_index": peak_index,
            "direct_peak_dbfs": round(_dbfs(direct_peak), 2),
            "pre_arrival_floor_dbfs": round(_dbfs(floor_rms), 2),
            "direct_to_pre_arrival_db": round(
                _dbfs(direct_peak) - _dbfs(floor_rms),
                2,
            ),
            "pre_arrival_window_ms": [
                round(pre_start / self.cfg.sample_rate * 1000.0, 2),
                round(pre_end / self.cfg.sample_rate * 1000.0, 2),
            ],
        }

    def _repeatability_from_arrays(
        self,
        first: np.ndarray,
        repeat: np.ndarray,
        freqs_hz: np.ndarray,
    ) -> dict[str, Any]:
        """Compare two captures at the same physical mic position."""
        if first.shape != repeat.shape or first.shape != freqs_hz.shape:
            return {
                "available": False,
                "level": "unavailable",
                "reason": "repeat and original curves use different shapes",
            }
        mask = (freqs_hz >= 50.0) & (freqs_hz <= min(350.0, self.cfg.peq_f_high))
        if int(mask.sum()) < 3:
            return {
                "available": False,
                "level": "unavailable",
                "reason": "not enough points in the repeatability band",
            }
        delta = first[mask] - repeat[mask]
        abs_delta = np.abs(delta)
        rms_db = float(np.sqrt(np.mean(delta ** 2)))
        p95_abs_db = float(np.percentile(abs_delta, 95))
        max_abs_db = float(np.max(abs_delta))
        if rms_db <= 1.5 and p95_abs_db <= 3.0:
            level = "high"
        elif rms_db <= 2.5 and p95_abs_db <= 5.0:
            level = "medium"
        else:
            level = "low"
        issues: list[dict[str, Any]] = []
        if level == "low":
            issues.append({
                "code": "repeatability_low",
                "severity": "warn",
                "message": (
                    "same-position repeat capture differs enough to limit "
                    "assertive correction"
                ),
            })
        return {
            "available": True,
            "level": level,
            "band_hz": [50.0, min(350.0, self.cfg.peq_f_high)],
            "metrics": {
                "rms_db": round(rms_db, 2),
                "p95_abs_db": round(p95_abs_db, 2),
                "max_abs_db": round(max_abs_db, 2),
            },
            "issues": issues,
        }

    def _smooth_capture(
        self,
        captured_wav_path: Path,
        *,
        capture_kind: str,
        position_index: int | None,
    ) -> tuple[
        np.ndarray,
        np.ndarray,
        quality.CaptureQuality,
        dict[str, Any],
        dict[str, Any] | None,
    ]:
        """Read capture, assess quality, deconvolve, smooth, log-resample.

        Returns (log_freqs, smoothed_db, capture_quality) for both the
        design positions and the verify pass.
        """
        if self.sweep_meta is None:
            raise RuntimeError(
                "no sweep_meta — flow ordering bug (call _ensure_sweep_cache first)"
            )

        captured, sr = sweep.read_wav_mono(captured_wav_path)
        # Bound the capture once, here at the session boundary, so the
        # recorded capture quality and the deconvolved IR describe the
        # same signal (deconvolve re-caps, idempotently). Pass the
        # pre-cap length so assess_capture can surface a truncation
        # warning at /status / bundle / doctor, not just the journal.
        raw_capture_samples = len(captured)
        captured = deconv.cap_capture_length(
            captured,
            sweep_len=self.sweep_meta.n_samples,
            sample_rate=sr,
        )
        capture_quality = quality.assess_capture(
            captured,
            sample_rate=sr,
            expected_sample_rate=self.cfg.sample_rate,
            sweep_n_samples=self.sweep_meta.n_samples,
            has_mic_calibration=self.mic_calibration is not None,
            input_device=self.input_device,
            truncated_from_samples=raw_capture_samples,
            quality_model=ROOM_QUALITY,
        )
        for issue in capture_quality.issues:
            logger.warning(
                "capture_quality session=%s code=%s severity=%s detail=%s",
                self.session_id, issue.code, issue.severity, issue.message,
            )
        if capture_quality.failed:
            raise quality.CaptureQualityError(capture_quality)
        sweep_signal, _ = sweep.synchronized_swept_sine(
            f1=self.sweep_meta.f1,
            f2=self.sweep_meta.f2,
            duration_approx_s=self.sweep_meta.duration_s,
            sample_rate=self.sweep_meta.sample_rate,
            amplitude_dbfs=self.sweep_meta.amplitude_dbfs,
        )
        ir = deconv.deconvolve(
            captured.astype(np.float64),
            sweep_signal.astype(np.float64),
            sample_rate=self.cfg.sample_rate,
        )
        direct_arrival = self._direct_arrival_report(ir)
        freqs, mag_db = deconv.magnitude_response(
            ir,
            self.cfg.sample_rate,
            normalize=False,
        )
        smoothed = analysis.smooth_fractional_octave(freqs, mag_db, fraction=48)
        log_freqs, log_mag = analysis.resample_log(freqs, smoothed)
        if self.mic_calibration is not None:
            log_mag = calibration.apply_calibration_curve(
                log_freqs, log_mag, self.mic_calibration.curve,
            )
        log_mag = analysis.normalize_to_band(
            log_freqs,
            log_mag,
            f_low=ANALYSIS_NORMALIZE_BAND_HZ[0],
            f_high=ANALYSIS_NORMALIZE_BAND_HZ[1],
        )
        replay_artifact_info = self._write_capture_replay_artifacts(
            captured_wav_path,
            capture_kind=capture_kind,
            position_index=position_index,
            ir=ir,
            raw_freqs_hz=freqs,
            raw_magnitude_db=mag_db,
            smoothed_magnitude_db=smoothed,
            log_freqs_hz=log_freqs,
            log_magnitude_db=log_mag,
            direct_arrival=direct_arrival,
        )
        return (
            log_freqs,
            log_mag,
            capture_quality,
            direct_arrival,
            replay_artifact_info,
        )

    def _quality_report_dict(
        self,
        report: quality.CaptureQuality,
        *,
        capture_kind: str,
        captured_wav_path: Path,
        position_index: int | None = None,
        noise_report: dict[str, Any] | None = None,
        direct_arrival: dict[str, Any] | None = None,
        replay_artifacts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        out = report.to_dict()
        out["capture_kind"] = capture_kind
        out["position_index"] = position_index
        artifact_path = captured_wav_path
        if self.bundle_dir is not None:
            try:
                artifact_path = captured_wav_path.relative_to(self.bundle_dir)
            except ValueError:
                pass
        out["artifact_path"] = str(artifact_path)
        source_noise_floor = None
        source_method = None
        if noise_report and noise_report.get("rms_dbfs") is not None:
            source_noise_floor = float(noise_report["rms_dbfs"])
            source_method = str(noise_report.get("method") or "noise_capture")
            out["noise_artifact_path"] = noise_report.get("artifact_path")
        elif self.noise_floor_db is not None and np.isfinite(self.noise_floor_db):
            source_noise_floor = float(self.noise_floor_db)
            source_method = "browser_autolevel_scalar"
        if source_noise_floor is not None:
            estimated_snr_db = float(report.rms_dbfs - source_noise_floor)
            out["noise_floor_dbfs"] = round(source_noise_floor, 2)
            out["noise_floor_method"] = source_method
            out["estimated_snr_db"] = round(estimated_snr_db, 2)
            band_snr = self._capture_band_snr(captured_wav_path, noise_report)
            if band_snr:
                out["band_snr"] = band_snr
            if estimated_snr_db < 20.0:
                issues = list(out.get("issues") or [])
                issues.append({
                    "code": "capture_snr_low",
                    "severity": "warn",
                    "message": (
                        "capture is less than 20 dB above the measured "
                        "pre-sweep noise floor"
                    ),
                    "details": {
                        "estimated_snr_db": round(estimated_snr_db, 2),
                        "threshold_db": 20.0,
                    },
                })
                out["issues"] = issues
        if direct_arrival is not None:
            out["direct_arrival"] = direct_arrival
        if replay_artifacts is not None:
            out["replay_artifacts"] = replay_artifacts
        return out

    def _design_target(self, freqs: np.ndarray) -> np.ndarray:
        """Resolve target_choice → dB target curve on `freqs`."""
        return strategy.resolve_target_profile(self.target_choice).curve_db(freqs)

    def _compute_verify_before_after(
        self,
        verify_freqs: np.ndarray,
        verify_mag_db: np.ndarray,
        target_db: np.ndarray,
    ) -> dict[str, Any] | None:
        """Honest MEASURED before/after over the verify band.

        The "before" is the pre-correction spatial-averaged measured
        curve (`self.measured_curve`); the "after" is the just-captured
        verify curve. Both deviations are taken over the SAME band as
        `verify_metrics` (`[50, self.cfg.peq_f_high]` — see the comment
        at the verify_metrics call for why 50 Hz, not the 20 Hz PEQ
        band), which is the guard against the band-mismatch trap: we
        deliberately do NOT reuse the design report's predicted "before"
        (computed over the 20–350/20–500 strategy band) as the measured
        baseline.

        Every capture resamples onto the same log grid, so the design
        measured curve and the verify curve share `verify_freqs`. We
        still interpolate onto `verify_freqs` defensively so a future
        grid change can't silently misalign the two arrays. Returns None
        if the pre-correction measured curve is unavailable.
        """
        if self.measured_curve is None:
            return None
        pre_freqs = np.asarray(self.measured_curve.freqs_hz, dtype=np.float64)
        pre_mag = np.asarray(self.measured_curve.magnitude_db, dtype=np.float64)
        if pre_freqs.size == 0 or pre_mag.size != pre_freqs.size:
            return None
        # Align the pre-correction curve to the verify grid. np.interp is
        # a no-op when the grids already match (the normal case).
        before_on_grid = np.interp(verify_freqs, pre_freqs, pre_mag)
        return analysis.before_after_delta(
            verify_freqs,
            before_on_grid,
            verify_mag_db,
            target_db,
            f_high=self.cfg.peq_f_high,
        )

    def _evaluate_acceptance(
        self,
        verify_freqs: np.ndarray,
        verify_mag_db: np.ndarray,
        target_db: np.ndarray,
    ) -> dict[str, Any] | None:
        """Run the deterministic P4 acceptance verdict for this verify.

        The pure :func:`acceptance.evaluate_acceptance` decides accept /
        surface / revert_pending_confirm / revert from the MEASURED before/
        after (never the prediction). The matched comparison basis is the
        pre-correction **position-1** curve (same geometry as the verify);
        the spatial-average ``measured_curve`` is the fallback for a session
        with no retained position-1 curve.

        This method owns the confirmatory-re-measure concordance state: it
        increments ``_verify_count`` and passes the prior clear-regression flag
        so a clear regression only escalates from ``revert_pending_confirm`` to
        ``revert`` when the verify IMMEDIATELY AFTER it concurs. Adjacency is
        strict: a clean verify ANSWERS the pending question (the first read was
        noise) and clears the flag, so a later regression starts a fresh
        pending-confirm cycle rather than firing an instant revert off a stale
        flag — the household was promised "measure once more to be sure", and
        that promise holds for every regression.

        Fail-soft: recoverable computation errors return ``None`` (the verdict
        is simply absent) so the acceptance verdict can never break the verify
        analysis path. The catch is the named ``RECOVERABLE_ERRORS`` family
        (P2's precedent in :mod:`jasper.audio_measurement.ramp`), not a blind
        except — the evaluator itself already degrades malformed inputs to a
        ``surface`` verdict structurally.
        """
        try:
            if self.position1_curve is not None:
                basis_freqs = np.asarray(
                    self.position1_curve.freqs_hz, dtype=np.float64,
                )
                basis_mag = np.asarray(
                    self.position1_curve.magnitude_db, dtype=np.float64,
                )
                basis = "position_1"
            elif self.measured_curve is not None:
                basis_freqs = np.asarray(
                    self.measured_curve.freqs_hz, dtype=np.float64,
                )
                basis_mag = np.asarray(
                    self.measured_curve.magnitude_db, dtype=np.float64,
                )
                basis = "spatial_average"
            else:
                return None
            if basis_freqs.size == 0 or basis_mag.size != basis_freqs.size:
                return None

            # Align the pre-correction basis to the verify grid (a no-op when
            # the grids already match — the normal case).
            before_on_grid = np.interp(verify_freqs, basis_freqs, basis_mag)

            self._verify_count += 1
            result = acceptance.evaluate_acceptance(
                freqs=verify_freqs,
                before_db=before_on_grid,
                verify_db=verify_mag_db,
                target_db=target_db,
                f_high=self.cfg.peq_f_high,
                basis=basis,
                verify_index=self._verify_count,
                prior_clear_regression=self._prior_clear_regression,
            )
            # Record this verify's clear-regression state so the NEXT verify
            # can judge concordance. STRICT ADJACENCY: a clean verify clears
            # the flag — the confirmatory sweep the flow asked for has
            # answered the pending question (first read = noise), so a later
            # regression must earn its own confirmatory re-measure rather
            # than reverting instantly off a stale flag. (Latched semantics
            # compound the single-sweep false-flag rate across re-verifies;
            # adjacency squares it — the plan's false-revert-is-trust-
            # expensive axis.)
            self._prior_clear_regression = result.clear_regression
            return result.to_dict()
        except RECOVERABLE_ERRORS:
            logger.exception("acceptance verdict computation failed")
            return None

    def _build_confidence_report(self) -> dict[str, Any]:
        return confidence.build_confidence_report(
            total_positions=self.total_positions,
            completed_positions=len(self.position_magnitudes),
            has_mic_calibration=self.mic_calibration is not None,
            input_device=self.input_device,
            capture_quality=self.capture_quality,
            strategy_choice=self.strategy_choice,
            browser_audio_report=self.browser_audio_report,
            runtime_integrity=self.runtime_integrity.summary(),
            repeatability_report=self.repeatability_report,
            position_magnitudes=self.position_magnitudes,
            freqs_hz=self.position_freqs,
            correction_band_hz=(self.cfg.peq_f_low, self.cfg.peq_f_high),
        )

    # ------------------------------------------------------------------
    # Phase 1 / Phase 2 measurement flow.
    # ------------------------------------------------------------------

    async def begin_noise_capture(self) -> None:
        """Ask the browser to record pre-sweep room noise.

        The browser flow uses this before each measurement position so
        the bundle carries a real noise artifact instead of only the
        older autolevel scalar. Direct test/legacy callers may still
        call `prepare_and_play_sweep()` from IDLE.
        """
        async with self._lock:
            valid_states = {SessionState.IDLE, SessionState.NEEDS_NEXT_POSITION}
            if self.state not in valid_states:
                raise RuntimeError(
                    f"cannot start noise capture from state {self.state.value}"
                )
            await self._set_state(
                SessionState.NEEDS_NOISE_CAPTURE,
                position=self.current_position,
                total_positions=self.total_positions,
            )

    async def on_noise_capture_uploaded(self, noise_wav_path: Path) -> None:
        """Persist the pre-sweep silence WAV and derive noise floors."""
        async with self._lock:
            if self.state != SessionState.NEEDS_NOISE_CAPTURE:
                raise RuntimeError(
                    f"cannot accept noise capture from state {self.state.value}"
                )
            position_index = self.current_position

        self._record_raw_capture_artifact(
            noise_wav_path,
            capture_kind="noise",
            position_index=position_index,
        )
        report = self._noise_report_dict(
            noise_wav_path,
            position_index=position_index,
        )
        self.noise_reports = [
            r for r in self.noise_reports
            if r.get("position_index") != position_index
        ]
        self.noise_reports.append(report)
        self.noise_floor_db = report.get("rms_dbfs")
        self._refresh_acoustic_quality()
        try:
            self._write_acoustic_quality_json()
            self._write_info_json()
        except Exception:  # noqa: BLE001
            logger.exception("bundle noise capture artifact write failed")

    async def prepare_and_play_sweep(
        self,
        play_sweep_async: Callable[..., Awaitable[Any]],
        *,
        alsa_device: str | None = None,
        runtime_probe_async: (
            Callable[[], Awaitable[dict[str, Any] | None]] | None
        ) = None,
    ) -> None:
        """Single sweep. Used both for position[i] within a multi-
        position flow AND for the Phase 1 single-position-only path.

        Flow: PREPARING → SWEEPING → AWAITING_CAPTURE.

        The caller is responsible for the measurement_window —
        either wrapping a single call (Phase 1 single-position) or
        opening once and calling this multiple times (multi-position).
        """
        async with self._lock:
            valid_states = {
                SessionState.IDLE, SessionState.READY,
                SessionState.APPLIED, SessionState.FAILED,
                SessionState.VERIFIED,
                SessionState.NEEDS_NOISE_CAPTURE,
                SessionState.NEEDS_NEXT_POSITION,
            }
            if self.state not in valid_states:
                raise RuntimeError(
                    f"cannot start sweep from state {self.state.value}"
                )
            await self._set_state(
                SessionState.PREPARING,
                position=self.current_position,
                total_positions=self.total_positions,
            )
            position_index = self.current_position

        await self._record_runtime_snapshot(
            "measurement_prepare",
            capture_kind="measurement",
            position_index=position_index,
            runtime_probe_async=runtime_probe_async,
        )

        try:
            sweep_wav, meta = self._ensure_sweep_cache()
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"sweep generation failed: {e}")
            raise

        async with self._lock:
            await self._set_state(
                SessionState.SWEEPING,
                duration_s=meta.duration_s,
                position=self.current_position,
                total_positions=self.total_positions,
            )

        try:
            kwargs = {"alsa_device": alsa_device} if alsa_device else {}
            await self._record_runtime_snapshot(
                "measurement_sweep_start",
                capture_kind="measurement",
                position_index=position_index,
                runtime_probe_async=runtime_probe_async,
            )
            await play_sweep_async(str(sweep_wav), **kwargs)
            await self._record_runtime_snapshot(
                "measurement_sweep_complete",
                capture_kind="measurement",
                position_index=position_index,
                runtime_probe_async=runtime_probe_async,
            )
        except Exception as e:  # noqa: BLE001
            await self._record_runtime_snapshot(
                "measurement_sweep_failed",
                capture_kind="measurement",
                position_index=position_index,
                runtime_probe_async=runtime_probe_async,
            )
            async with self._lock:
                await self._fail(f"sweep playback failed: {e}")
            raise

        async with self._lock:
            await self._set_state(
                SessionState.AWAITING_CAPTURE,
                position=self.current_position,
                total_positions=self.total_positions,
            )

    async def prepare_and_play_repeat_sweep(
        self,
        play_sweep_async: Callable[..., Awaitable[Any]],
        *,
        alsa_device: str | None = None,
        runtime_probe_async: (
            Callable[[], Awaitable[dict[str, Any] | None]] | None
        ) = None,
    ) -> None:
        """Play an optional repeat sweep at the main seat.

        This uses the same sweep and measurement window as a normal
        position but stores the resulting capture separately so bundle
        recompute does not mistake it for another listening position.
        """
        async with self._lock:
            if self.state != SessionState.NEEDS_REPEAT_CAPTURE:
                raise RuntimeError(
                    f"cannot start repeat sweep from state {self.state.value}"
                )
            position_index = 0
            await self._set_state(
                SessionState.PREPARING,
                position=position_index,
                total_positions=self.total_positions,
            )

        await self._record_runtime_snapshot(
            "repeat_prepare",
            capture_kind="repeat",
            position_index=position_index,
            runtime_probe_async=runtime_probe_async,
        )

        try:
            sweep_wav, meta = self._ensure_sweep_cache()
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"sweep generation failed: {e}")
            raise

        async with self._lock:
            await self._set_state(
                SessionState.SWEEPING,
                duration_s=meta.duration_s,
                position=position_index,
                total_positions=self.total_positions,
            )

        try:
            kwargs = {"alsa_device": alsa_device} if alsa_device else {}
            await self._record_runtime_snapshot(
                "repeat_sweep_start",
                capture_kind="repeat",
                position_index=position_index,
                runtime_probe_async=runtime_probe_async,
            )
            await play_sweep_async(str(sweep_wav), **kwargs)
            await self._record_runtime_snapshot(
                "repeat_sweep_complete",
                capture_kind="repeat",
                position_index=position_index,
                runtime_probe_async=runtime_probe_async,
            )
        except Exception as e:  # noqa: BLE001
            await self._record_runtime_snapshot(
                "repeat_sweep_failed",
                capture_kind="repeat",
                position_index=position_index,
                runtime_probe_async=runtime_probe_async,
            )
            async with self._lock:
                await self._fail(f"repeat sweep playback failed: {e}")
            raise

        async with self._lock:
            await self._set_state(
                SessionState.AWAITING_REPEAT_CAPTURE,
                position=position_index,
                total_positions=self.total_positions,
            )

    async def on_capture_uploaded(
        self, captured_wav_path: Path,
    ) -> None:
        """Position-N capture arrived. Deconv + smooth + store. If
        more positions remain, transition to NEEDS_NEXT_POSITION.
        Otherwise, spatial-average the per-position magnitudes,
        design PEQs, transition to READY."""
        async with self._lock:
            if self.state != SessionState.AWAITING_CAPTURE:
                raise RuntimeError(
                    f"cannot accept capture from state {self.state.value}"
                )
            await self._set_state(
                SessionState.ANALYZING,
                position=self.current_position,
            )
            self.last_capture_path = captured_wav_path
            position_index = self.current_position

        self._record_raw_capture_artifact(
            captured_wav_path,
            capture_kind="measurement",
            position_index=position_index,
        )
        self._record_runtime_capture(
            captured_wav_path,
            capture_kind="measurement",
            position_index=position_index,
        )
        noise_report = self._noise_report_for_position(position_index)

        # Deconvolution + smoothing (and, below, PEQ design) are
        # multi-second NumPy. Run them on a worker thread so they don't
        # monopolize the single shared correction event loop (other
        # measurement coroutines schedule onto it) for the whole
        # analysis. Safe without extra locking: ANALYZING is an active,
        # reset-busy state with the capture watchdog disarmed, so no
        # other coroutine mutates this session while the worker runs.
        try:
            (
                log_freqs,
                log_mag,
                capture_quality,
                direct_arrival,
                replay_artifact_info,
            ) = await asyncio.to_thread(
                self._smooth_capture,
                captured_wav_path,
                capture_kind="measurement",
                position_index=position_index,
            )
        except Exception as e:  # noqa: BLE001
            if isinstance(e, quality.CaptureQualityError):
                self.capture_quality.append(self._quality_report_dict(
                    e.report,
                    capture_kind="measurement",
                    captured_wav_path=captured_wav_path,
                    position_index=position_index,
                    noise_report=noise_report,
                ))
                self._refresh_acoustic_quality()
                try:
                    self._write_acoustic_quality_json()
                except Exception:  # noqa: BLE001
                    logger.exception("bundle acoustic_quality.json write failed")
            async with self._lock:
                await self._fail(f"analysis failed: {e}")
            raise
        await self._record_runtime_snapshot(
            "measurement_analysis_complete",
            capture_kind="measurement",
            position_index=position_index,
            runtime_probe_async=None,
        )

        if self.position_freqs is None:
            self.position_freqs = log_freqs
        self.position_magnitudes.append(log_mag)
        self.capture_quality.append(self._quality_report_dict(
            capture_quality,
            capture_kind="measurement",
            captured_wav_path=captured_wav_path,
            position_index=position_index,
            noise_report=noise_report,
            direct_arrival=direct_arrival,
            replay_artifacts=replay_artifact_info,
        ))
        self._refresh_acoustic_quality()
        try:
            self._write_acoustic_quality_json()
        except Exception:  # noqa: BLE001
            logger.exception("bundle acoustic_quality.json write failed")
        self.current_position += 1

        if (
            self.repeat_main_position
            and position_index == 0
            and self.repeat_quality is None
        ):
            async with self._lock:
                await self._set_state(
                    SessionState.NEEDS_REPEAT_CAPTURE,
                    position=0,
                    total_positions=self.total_positions,
                )
            return

        if self.current_position < self.total_positions:
            # Wait for the user to move to the next position.
            async with self._lock:
                await self._set_state(
                    SessionState.NEEDS_NEXT_POSITION,
                    position=self.current_position,
                    total_positions=self.total_positions,
                )
            return

        # All positions captured. Average + design.
        try:
            await asyncio.to_thread(self._run_design_from_positions)
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"PEQ design failed: {e}")
            raise

        try:
            self._write_result_json()
        except Exception:  # noqa: BLE001
            logger.exception("bundle result.json write failed")

        async with self._lock:
            await self._set_state(
                SessionState.READY,
                peq_count=len(self.peqs),
                positions_used=self.total_positions,
            )

    async def on_repeat_capture_uploaded(
        self,
        captured_wav_path: Path,
    ) -> None:
        """Same-position repeat capture arrived for trust scoring."""
        async with self._lock:
            if self.state != SessionState.AWAITING_REPEAT_CAPTURE:
                raise RuntimeError(
                    f"cannot accept repeat capture from state {self.state.value}"
                )
            await self._set_state(
                SessionState.ANALYZING,
                position=0,
            )
            self.last_capture_path = captured_wav_path

        self._record_raw_capture_artifact(
            captured_wav_path,
            capture_kind="repeat",
            position_index=0,
        )
        self._record_runtime_capture(
            captured_wav_path,
            capture_kind="repeat",
            position_index=0,
        )
        noise_report = self._noise_report_for_position(0)

        try:
            (
                log_freqs,
                log_mag,
                capture_quality,
                direct_arrival,
                replay_artifact_info,
            ) = await asyncio.to_thread(
                self._smooth_capture,
                captured_wav_path,
                capture_kind="repeat",
                position_index=0,
            )
        except Exception as e:  # noqa: BLE001
            if isinstance(e, quality.CaptureQualityError):
                self.repeat_quality = self._quality_report_dict(
                    e.report,
                    capture_kind="repeat",
                    captured_wav_path=captured_wav_path,
                    position_index=0,
                    noise_report=noise_report,
                )
                self._refresh_acoustic_quality()
                try:
                    self._write_acoustic_quality_json()
                except Exception:  # noqa: BLE001
                    logger.exception("bundle acoustic_quality.json write failed")
            async with self._lock:
                await self._fail(f"repeat analysis failed: {e}")
            raise

        await self._record_runtime_snapshot(
            "repeat_analysis_complete",
            capture_kind="repeat",
            position_index=0,
            runtime_probe_async=None,
        )
        self.repeat_curve = CurveJSON(
            freqs_hz=log_freqs.tolist(),
            magnitude_db=log_mag.tolist(),
        )
        self.repeat_quality = self._quality_report_dict(
            capture_quality,
            capture_kind="repeat",
            captured_wav_path=captured_wav_path,
            position_index=0,
            noise_report=noise_report,
            direct_arrival=direct_arrival,
            replay_artifacts=replay_artifact_info,
        )
        if self.position_freqs is not None and self.position_magnitudes:
            self.repeatability_report = self._repeatability_from_arrays(
                self.position_magnitudes[0],
                log_mag,
                self.position_freqs,
            )
        else:
            self.repeatability_report = {
                "available": False,
                "level": "unavailable",
                "reason": "original main-seat capture is unavailable",
            }
        self._refresh_acoustic_quality()
        try:
            self._write_acoustic_quality_json()
        except Exception:  # noqa: BLE001
            logger.exception("bundle acoustic_quality.json write failed")

        if self.current_position < self.total_positions:
            async with self._lock:
                await self._set_state(
                    SessionState.NEEDS_NEXT_POSITION,
                    position=self.current_position,
                    total_positions=self.total_positions,
                )
            return

        try:
            await asyncio.to_thread(self._run_design_from_positions)
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"PEQ design failed: {e}")
            raise

        try:
            self._write_result_json()
        except Exception:  # noqa: BLE001
            logger.exception("bundle result.json write failed")

        async with self._lock:
            await self._set_state(
                SessionState.READY,
                peq_count=len(self.peqs),
                positions_used=self.total_positions,
            )

    def _run_design_from_positions(self) -> None:
        """Spatial-average per-position magnitudes, run target lookup,
        run PEQ design, fill measured/target/predicted curves."""
        if not self.position_magnitudes or self.position_freqs is None:
            raise RuntimeError(
                "no position data — run capture first"
            )

        averaged_db = analysis.spatial_average_db(self.position_magnitudes)
        log_freqs = self.position_freqs
        # Read the active bass-management corner (fail-soft None). The room
        # designer READS it — it never re-picks it (the speaker layer owns the
        # corner) — so it can refuse to boost inside the crossover region.
        from jasper.bass_management import active_crossover_corner_hz

        design = strategy.design_correction(
            averaged_db,
            log_freqs,
            target_choice=self.target_choice,
            strategy_choice=self.strategy_choice,
            position_magnitudes=self.position_magnitudes,
            crossover_hz=active_crossover_corner_hz(),
        )

        self.measured_curve = CurveJSON(
            freqs_hz=log_freqs.tolist(),
            magnitude_db=averaged_db.tolist(),
        )
        # Retain position 1 (the first captured seat) on its own so the P4
        # verify can compare against the SAME geometry it re-measures at,
        # rather than only against the multi-seat spatial average.
        self.position1_curve = CurveJSON(
            freqs_hz=log_freqs.tolist(),
            magnitude_db=self.position_magnitudes[0].tolist(),
        )
        self.target_curve = CurveJSON(
            freqs_hz=log_freqs.tolist(),
            magnitude_db=design.target_db.tolist(),
        )
        self.predicted_curve = CurveJSON(
            freqs_hz=log_freqs.tolist(),
            magnitude_db=design.predicted_db.tolist(),
        )
        self.peqs = [PEQJSON.from_peq(p) for p in design.peqs]
        self.design_report = design.report
        self.confidence_report = self._build_confidence_report()
        self.design_report["confidence_report"] = self.confidence_report
        try:
            self._write_position_analysis_json()
        except Exception:  # noqa: BLE001
            self.position_analysis = None
            logger.exception("bundle position_analysis.json write failed")

    # ------------------------------------------------------------------
    # Apply / reset / verify.
    # ------------------------------------------------------------------

    async def apply(
        self,
        camilla_set_config: Callable[[str], Awaitable[bool]],
        camilla_get_config: Callable[[], Awaitable[str | None]] | None = None,
    ) -> None:
        async with self._lock:
            if self.state != SessionState.READY:
                raise RuntimeError(
                    f"cannot apply from state {self.state.value}"
                )

        try:
            self.cfg.config_dir.mkdir(parents=True, exist_ok=True)
            out_path = self.cfg.config_dir / (
                f"correction_{self.session_id}_{int(self.started_at)}.yml"
            )
            peq_objs = [
                PEQ(freq=p.freq_hz, q=p.q, gain=p.gain_db)
                for p in self.peqs
            ]
            from jasper.multiroom.member_config import member_camilla_kwargs
            from jasper.sound.camilla_yaml import emit_sound_config
            from jasper.sound.profile import build_sound_filters, load_profile
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"YAML emit failed: {e}")
            raise

        async def _prepare_config() -> dict[str, Any]:
            profile = load_profile()
            prior_config_path: str | None = None
            if camilla_get_config is not None:
                from jasper.fanin_coupling import coupling_capture_kwargs_from_env
                from jasper.sound.graph_carrier import carrier_for_loaded_config

                prior_config_path = await camilla_get_config()
                if not prior_config_path:
                    raise RuntimeError(
                        "CamillaDSP did not report a loaded config path"
                    )
                # Remember the pre-swap graph so a P4 confirmed-regression
                # auto-revert can restore it via the existing reset() path.
                self.pre_apply_config_path = prior_config_path
                carrier = carrier_for_loaded_config(
                    prior_config_path,
                    config_dir=self.cfg.config_dir,
                )
                result = carrier.reemit(
                    profile,
                    room_peqs=peq_objs,
                    out_path=out_path,
                    profile_id=self.session_id,
                    fanin_coupling_capture_kwargs=coupling_capture_kwargs_from_env(),
                )
                from jasper.correction.runtime_safety import (
                    assert_correction_graph_safe,
                )

                assert_correction_graph_safe(result.yaml)
            else:
                # Compatibility for tests and older direct callers that provide
                # only a setter. The web surface always passes camilla_get_config
                # and therefore uses the topology-aware carrier above.
                from jasper.correction.runtime_safety import assert_flat_apply_safe

                assert_flat_apply_safe()
                emit_sound_config(
                    profile,
                    room_peqs=peq_objs,
                    out_path=out_path,
                    profile_id=self.session_id,
                    **member_camilla_kwargs(),
                )
            return {
                "prior_config_path": prior_config_path,
                "room_peq_count": len(peq_objs),
                "sound_filter_count": len(build_sound_filters(profile)),
            }

        try:
            from jasper.dsp_apply import DspApplyError, apply_dsp_config
            await apply_dsp_config(
                source="correction",
                candidate_path=out_path,
                load_config=camilla_set_config,
                get_current_config_path=camilla_get_config,
                prepare=_prepare_config,
                room_peq_count=len(peq_objs),
            )
            self.config_path = out_path
        except DspApplyError as e:
            if e.state.result == "prepare_failed":
                async with self._lock:
                    await self._fail(f"YAML emit failed: {e}")
                from jasper.sound.graph_carrier import CarrierCannotHostEq
                from jasper.correction.runtime_safety import (
                    CorrectionRuntimeSafetyError,
                )

                if isinstance(
                    e.__cause__,
                    (CarrierCannotHostEq, CorrectionRuntimeSafetyError),
                ):
                    raise e.__cause__ from e
                raise
            async with self._lock:
                await self._fail(f"CamillaDSP reload failed: {e}")
            if e.state.load_error == "CamillaDSP rejected candidate config path":
                return
            raise
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"CamillaDSP reload failed: {e}")
            raise

        try:
            self._copy_applied_yaml()
        except Exception:  # noqa: BLE001
            logger.exception("bundle applied.yml copy failed")

        async with self._lock:
            await self._set_state(
                SessionState.APPLIED,
                config_path=str(out_path),
            )

    async def reset(
        self,
        camilla_set_config: Callable[[str], Awaitable[bool]],
        *,
        target_config_path: str | Path | None = None,
    ) -> None:
        async with self._lock:
            if self._state_guard.is_reset_busy(self.state):
                raise SessionBusyError(
                    f"cannot reset while {self.state.value} — a sweep or "
                    "analysis is in progress; wait for it to finish"
                )
        try:
            reset_path = Path(target_config_path or self.cfg.base_config_path)
            ok = await camilla_set_config(str(reset_path))
            if not ok:
                async with self._lock:
                    await self._fail(
                        "CamillaDSP rejected the base config — manual "
                        "intervention required"
                    )
                return
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"reset reload failed: {e}")
            raise

        async with self._lock:
            await self._set_state(
                SessionState.IDLE,
                rolled_back_to=str(reset_path),
            )

    async def start_verify_sweep(
        self,
        play_sweep_async: Callable[..., Awaitable[Any]],
        *,
        alsa_device: str | None = None,
        runtime_probe_async: (
            Callable[[], Awaitable[dict[str, Any] | None]] | None
        ) = None,
    ) -> None:
        """One-position re-measurement after Apply. The result lands
        in self.verify_curve / self.verify_metrics, plus the honest
        MEASURED before/after readout in self.verify_before_after (the
        pre-correction measured curve vs this verify curve over the same
        band) — overlaid on the chart so the user can see the
        correction's actual, measured effect, not just the prediction."""
        async with self._lock:
            if self.state != SessionState.APPLIED and self.state != SessionState.VERIFIED:
                raise RuntimeError(
                    f"cannot verify from state {self.state.value}"
                )
            await self._set_state(SessionState.VERIFYING)

        await self._record_runtime_snapshot(
            "verify_prepare",
            capture_kind="verify",
            position_index=None,
            runtime_probe_async=runtime_probe_async,
        )

        try:
            sweep_wav, _ = self._ensure_sweep_cache()
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"sweep generation failed: {e}")
            raise

        async with self._lock:
            await self._set_state(SessionState.SWEEPING)

        try:
            kwargs = {"alsa_device": alsa_device} if alsa_device else {}
            await self._record_runtime_snapshot(
                "verify_sweep_start",
                capture_kind="verify",
                position_index=None,
                runtime_probe_async=runtime_probe_async,
            )
            await play_sweep_async(str(sweep_wav), **kwargs)
            await self._record_runtime_snapshot(
                "verify_sweep_complete",
                capture_kind="verify",
                position_index=None,
                runtime_probe_async=runtime_probe_async,
            )
        except Exception as e:  # noqa: BLE001
            await self._record_runtime_snapshot(
                "verify_sweep_failed",
                capture_kind="verify",
                position_index=None,
                runtime_probe_async=runtime_probe_async,
            )
            async with self._lock:
                await self._fail(f"verify sweep playback failed: {e}")
            raise

        async with self._lock:
            await self._set_state(SessionState.AWAITING_VERIFY_CAPTURE)

    async def on_verify_capture_uploaded(
        self, captured_wav_path: Path,
    ) -> None:
        """Verify capture arrived. Deconv + smooth, store as
        verify_curve, compute absolute deviation metrics AND the
        honest measured before/after delta (verify_before_after).
        Transition to VERIFIED."""
        async with self._lock:
            if self.state != SessionState.AWAITING_VERIFY_CAPTURE:
                raise RuntimeError(
                    f"cannot accept verify capture from state {self.state.value}"
                )
            await self._set_state(SessionState.ANALYZING)
            self.last_capture_path = captured_wav_path

        self._record_raw_capture_artifact(
            captured_wav_path,
            capture_kind="verify",
        )
        self._record_runtime_capture(
            captured_wav_path,
            capture_kind="verify",
            position_index=None,
        )

        try:
            (
                log_freqs,
                log_mag,
                capture_quality,
                direct_arrival,
                replay_artifact_info,
            ) = await asyncio.to_thread(
                self._smooth_capture,
                captured_wav_path,
                capture_kind="verify",
                position_index=None,
            )
        except Exception as e:  # noqa: BLE001
            if isinstance(e, quality.CaptureQualityError):
                self.verify_quality = self._quality_report_dict(
                    e.report,
                    capture_kind="verify",
                    captured_wav_path=captured_wav_path,
                )
                self._refresh_acoustic_quality()
                try:
                    self._write_acoustic_quality_json()
                except Exception:  # noqa: BLE001
                    logger.exception("bundle acoustic_quality.json write failed")
            async with self._lock:
                await self._fail(f"verify analysis failed: {e}")
            raise
        await self._record_runtime_snapshot(
            "verify_analysis_complete",
            capture_kind="verify",
            position_index=None,
            runtime_probe_async=None,
        )

        target_db = self._design_target(log_freqs)
        # Use deviation_metrics' DEFAULT band (50-350 Hz) rather than
        # the PEQ design band (20-350 Hz). Below ~50 Hz the iPhone
        # mic's built-in 24 dB/octave HPF dominates the captured
        # signal — including those frequencies in the deviation
        # summary produces alarming numbers ("max 56 dB!") that are
        # mic artifacts, not room reality. PEQ design still goes
        # down to 20 Hz because the mic captures *enough* there
        # to inform a useful filter, just not enough for a clean
        # deviation-from-target readout.
        metrics = analysis.deviation_metrics(
            log_mag, target_db, log_freqs,
            f_high=self.cfg.peq_f_high,
        )

        self.verify_curve = CurveJSON(
            freqs_hz=log_freqs.tolist(),
            magnitude_db=log_mag.tolist(),
        )
        self.verify_metrics = metrics
        self.verify_before_after = self._compute_verify_before_after(
            log_freqs, log_mag, target_db,
        )
        # P4: the deterministic accept/surface/revert verdict. Computed here
        # (pure — no CamillaDSP) and recorded on the session, in result.json
        # (below), and in the envelope. When the verdict is a CONFIRMED
        # regression (`revert`), the web layer performs the automatic rollback
        # through the existing reset() path — the session never writes
        # CamillaDSP itself.
        self.acceptance = self._evaluate_acceptance(
            log_freqs, log_mag, target_db,
        )
        if self.acceptance is not None:
            log_event(
                logger,
                "correction_acceptance.verdict",
                session=self.session_id,
                verdict=self.acceptance.get("verdict"),
                verify_index=self.acceptance.get("verify_index"),
                basis=self.acceptance.get("basis"),
                overall_rms_delta_db=self.acceptance.get(
                    "overall_rms_delta_db"
                ),
                regressed_band_count=self.acceptance.get(
                    "regressed_band_count"
                ),
                confirmed=self.acceptance.get("confirmed"),
                level=(
                    logging.WARNING
                    if self.acceptance.get("verdict")
                    in ("revert", "revert_pending_confirm")
                    else logging.INFO
                ),
            )
        self.verify_quality = self._quality_report_dict(
            capture_quality,
            capture_kind="verify",
            captured_wav_path=captured_wav_path,
            direct_arrival=direct_arrival,
            replay_artifacts=replay_artifact_info,
        )
        self._refresh_acoustic_quality()
        try:
            self._write_acoustic_quality_json()
        except Exception:  # noqa: BLE001
            logger.exception("bundle acoustic_quality.json write failed")

        try:
            self._write_result_json()
        except Exception:  # noqa: BLE001
            logger.exception("bundle result.json (verify) write failed")

        async with self._lock:
            await self._set_state(
                SessionState.VERIFIED,
                rms_db=metrics["rms_db"],
                max_db=metrics["max_db"],
            )
        # Verify ends the measurement without an apply/reset, so restore the
        # listening level here too if autolevel ramped it (e.g. autolevel was
        # re-run from the APPLIED state before this verify).
        await self._restore_listening_volume_if_ramped()

    @property
    def acceptance_verdict(self) -> str | None:
        """The current P4 verdict string, or None before a verify lands.

        A thin, side-effect-free accessor the web layer reads to decide
        whether to trigger the automatic rollback (``revert``) or prompt for a
        confirmatory re-measure (``revert_pending_confirm``).
        """
        if not isinstance(self.acceptance, dict):
            return None
        verdict = self.acceptance.get("verdict")
        return verdict if isinstance(verdict, str) else None

    async def auto_revert(
        self,
        camilla_set_config: Callable[[str], Awaitable[bool]],
        *,
        target_config_path: str | Path | None = None,
    ) -> bool:
        """Automatically roll back a CONFIRMED-regression correction.

        The one automatic action P4 takes against the household's applied
        choice. It fires only when the deterministic verdict is a *confirmed*
        clear regression (``revert`` — a second concordant verify, per plan §4
        P4 point 4); every other verdict (accept / surface /
        revert_pending_confirm) is a no-op here and returns False.

        The rollback rides the **existing** reset() reversal — this method
        never invents a new one. ``target_config_path`` is the graph to restore
        (the caller resolves it the same way ``POST /reset`` does: the no-room
        re-emit of the current topology, preserving speaker DSP + preference
        EQ); when the caller passes nothing it falls back to the pre-apply
        config captured at apply() time, then to the base graph inside reset().

        Returns True only when the rollback actually completed (session now
        IDLE on the restored graph). The outcome — ok or failed — is recorded
        on ``self.auto_revert_outcome`` when it is KNOWN, never predicted, so
        the envelope tells the household the truth even when the caller's HTTP
        response already went out (an upload-response timeout leaves this
        coroutine running; it records the result when reset() finishes). On a
        reset failure reset() itself also fails the session loudly (no silent
        revert failure).
        """
        if self.acceptance_verdict != "revert":
            return False
        target = target_config_path or self.pre_apply_config_path
        log_event(
            logger,
            "correction_acceptance.auto_revert",
            session=self.session_id,
            target=str(target) if target else None,
            worst_band_center_hz=(
                self.acceptance.get("worst_band_center_hz")
                if isinstance(self.acceptance, dict)
                else None
            ),
            overall_rms_delta_db=(
                self.acceptance.get("overall_rms_delta_db")
                if isinstance(self.acceptance, dict)
                else None
            ),
            level=logging.WARNING,
        )
        # try/finally (not try/except) so a raising reset() still records a
        # truthful "failed" outcome while the original exception propagates
        # untouched. reset() has two NON-raising terminal shapes: success →
        # IDLE (with rolled_back_to), or CamillaDSP rejected the config →
        # _fail → FAILED without raising. Only the first is a performed
        # rollback.
        ok = False
        try:
            await self.reset(camilla_set_config, target_config_path=target)
            ok = self.state == SessionState.IDLE
        finally:
            self._record_auto_revert_outcome("ok" if ok else "failed")
        return ok

    def _record_auto_revert_outcome(self, result: str) -> None:
        """Record the completed rollback outcome + rewrite the evidence.

        Called only from auto_revert() once the outcome is a fact. Runs inside
        its finally-block, so it must never raise: the dict assignment and the
        log line are non-raising, and the result.json rewrite is guarded (the
        bundle write must never mask the revert result). The `event=` line
        makes the outcome greppable next to the intent line auto_revert()
        already emitted.
        """
        self.auto_revert_outcome = {"result": result, "at": time.time()}
        log_event(
            logger,
            "correction_acceptance.auto_revert_outcome",
            session=self.session_id,
            result=result,
            level=logging.WARNING if result != "ok" else logging.INFO,
        )
        try:
            self._write_result_json()
        except RECOVERABLE_ERRORS:
            logger.exception("bundle result.json (auto-revert) write failed")

    # ------------------------------------------------------------------
    # Auto-level.
    # ------------------------------------------------------------------

    async def run_autolevel(
        self,
        *,
        get_main_volume_db: Callable[[], Awaitable[float]],
        set_main_volume_db: Callable[[float], Awaitable[Any]],
        play_continuous_tone: Callable[[], Awaitable[Any]],
        cancel_tone: Callable[[], None],
        start_db: float = -40.0,
        end_db: float | None = None,
        end_db_bump: float = 6.0,
        end_db_absolute_max: float = -6.0,
        end_db_absolute_min: float = -20.0,
        step_db: float = 1.0,
        step_interval_s: float = 0.15,
        safety_timeout_s: float = 25.0,
        fade_down_to_db: float = -40.0,
        fade_step_s: float = 0.03,
    ) -> None:
        """Auto-level CamillaDSP main_volume.

        Ramps main_volume from `start_db` up toward `end_db` while a
        continuous tone plays through the music chain. The client
        (iPhone) watches its mic level via AudioWorklet and either
        auto-locks when the captured level enters the target range,
        OR the user taps a manual "Lock now" button. Either path
        POSTs to `/autolevel/lock`, which sets
        the controller's lock event and causes this function to freeze
        main_volume at the current ramp value.

        Three exits:
          - LOCKED:     client signalled lock; main_volume stays at
                        the lock value.
          - MAXED_OUT:  ramp reached end_db without lock — speaker /
                        amp combo too quiet (or iOS Safari is silent-
                        AGC'ing the mic readout, which has happened
                        in the field). main_volume stays at end_db;
                        UI tells user to turn up amp OR use the
                        manual Lock button.
          - CANCELLED:  client called /autolevel/cancel OR safety
                        timeout fired. main_volume restored to
                        `original_main_volume_db`.

        Order of operations matters (a real first-user bug fix):
        we set main_volume to `start_db` BEFORE starting the tone
        so the user doesn't hear an initial blast at their normal
        listening level before the ramp drops them to -40 dB. And
        we fade main_volume back DOWN to `fade_down_to_db` before
        killing the tone, so the stop is silent rather than a click.

        On entry, snapshots current main_volume into
        `self.autolevel.original_main_volume_db` so the
        measurement-workflow apply/reset handlers can restore the
        user's listening volume after the workflow ends.

        Safety — DON'T BLOW THE LISTENER'S EARS OUT:

        Originally end_db was hard-capped at -6 dB. First-user
        report: even -6 dB was still way too loud — their listening
        volume was around -20 dB main_volume; -6 dB is 14 dB louder
        (~4x perceived loudness), painfully blasted them.

        end_db now defaults to None, computed RELATIVE TO the user's
        existing main_volume:

            end_db = clamp(
                original_main_volume_db + end_db_bump,
                [end_db_absolute_min, end_db_absolute_max],
            )

        Defaults give +6 dB bump over normal listening, clamped to
        [-20, -6] dB. So:
          - user at -20 dB → autolevel cap -14 dB (only ~6 dB louder)
          - user at -5 dB  → cap -6 dB (absolute max)
          - user at -45 dB → cap -20 dB (floored UP to a usable
            measurement level)

        Combined with the -12 dBFS tone amplitude (matches the
        sweep), worst-case dongle output at the cap is -18 dBFS —
        far quieter than the prior -6 dBFS tone × -6 dB cap = -12
        dBFS that blasted the user.
        """
        await self._autolevel_controller.run(
            get_main_volume_db=get_main_volume_db,
            set_main_volume_db=set_main_volume_db,
            play_continuous_tone=play_continuous_tone,
            cancel_tone=cancel_tone,
            start_db=start_db,
            end_db=end_db,
            end_db_bump=end_db_bump,
            end_db_absolute_max=end_db_absolute_max,
            end_db_absolute_min=end_db_absolute_min,
            step_db=step_db,
            step_interval_s=step_interval_s,
            safety_timeout_s=safety_timeout_s,
            fade_down_to_db=fade_down_to_db,
            fade_step_s=fade_step_s,
        )

    async def lock_autolevel(self) -> bool:
        """Signal the running autolevel task to stop ramping and
        lock at the current main_volume. Returns True if a task was
        running."""
        return await self._autolevel_controller.lock()

    async def cancel_autolevel(self) -> bool:
        """Signal the running autolevel task to abort and restore
        the original main_volume."""
        return await self._autolevel_controller.cancel()

    # ------------------------------------------------------------------
    # Level match (P2, relay-closed settle-based ramp).
    # ------------------------------------------------------------------

    async def run_level_match(
        self,
        geometry: str,
        *,
        get_main_volume_db: Callable[[], Awaitable[float]],
        set_main_volume_db: Callable[[float], Awaitable[Any]],
        play_continuous_tone: Callable[[], Awaitable[Any]],
        cancel_tone: Callable[[], None],
        read_status: Callable[[], dict[str, Any]],
        post_host_event: Callable[[dict[str, Any]], Any] | None = None,
        noise_floor_dbfs: float | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        run_token: str = "",
        wait_for_armed: bool = True,
        armed_timeout_s: float | None = None,
    ) -> LevelMatchOutcome:
        """Relay-closed, settle-based level match for one mic geometry (§3.1).

        The generalization of ``run_autolevel``: instead of the browser deciding
        the lock blind, the Pi's :class:`RampController` reads the phone's batched
        mic-level samples (via ``read_status``), recovers the chain gain, stops
        ahead into the safe window, and locks — never blasting up to find it. A
        terminal LOCKED / MAXED_OUT stores a per-geometry
        :class:`MeasurementLevelLock` in ``level_lock_store``.

        ``read_status`` is the relay status reader (the batched-event transport);
        the host injects it so this method never imports the relay client — and
        at production it must be a CACHED background-poller snapshot, never a
        blocking per-call HTTP GET (see level_match.py's P3b wiring notes). When
        no relay/phone session exists, the caller uses the existing
        ``run_autolevel`` local path instead — this method is additive, not a
        replacement. ``run_token`` is the per-run nonce minted into this run's
        ``build_level_ramp_spec``. ``clock`` / ``sleep`` default to the real
        asyncio clock; tests inject fakes.
        """
        loop = asyncio.get_running_loop()
        # Single-flight: one level match at a time per measurement session
        # (mirrors the /autolevel/start handler's "already in progress" guard —
        # AutolevelController is a PERMANENT controller so it needs no slot,
        # but this retained per-run session does). Without this, a second
        # overlapping run would stomp the retained slot and the first's clear
        # would then orphan the second's live ramp from its Lock/Cancel seam —
        # the one state where a dead Cancel matters (a live volume ramp).
        if self._level_match_session is not None:
            raise RuntimeError("level match already in progress")
        # Retain the run's session so a Lock/Cancel from the flow can reach the
        # running RampController while the ramp is in flight. The clear is
        # identity-guarded (belt and braces under the single-flight refusal):
        # only the run that owns the slot may empty it.
        session = LevelMatchSession(
            session_id=self.session_id,
            store=self.level_lock_store,
        )
        self._level_match_session = session
        try:
            outcome = await session.run_for_geometry(
                geometry,
                get_main_volume_db=get_main_volume_db,
                set_main_volume_db=set_main_volume_db,
                play_continuous_tone=play_continuous_tone,
                cancel_tone=cancel_tone,
                read_status=read_status,
                post_host_event=post_host_event,
                noise_floor_dbfs=noise_floor_dbfs,
                clock=clock if clock is not None else loop.time,
                sleep=sleep if sleep is not None else asyncio.sleep,
                run_token=run_token,
                wait_for_armed=wait_for_armed,
                armed_timeout_s=armed_timeout_s,
            )
        finally:
            if self._level_match_session is session:
                self._level_match_session = None
        self._last_level_match = outcome
        return outcome

    async def lock_level_match(self) -> bool:
        """Manual lock (the user tapped Lock during a level match) — freezes the
        running ramp at its current level and trusts the user. Returns True when
        a level match was in flight to lock, mirroring ``lock_autolevel``. A
        no-op returning False when no ramp is running."""
        session = self._level_match_session
        if session is None:
            return False
        return await session.lock_now()

    async def cancel_level_match(self) -> bool:
        """Signal a running level match to abort and restore the pre-ramp
        volume (the kernel owns the restore). Returns True when a level match
        was in flight, mirroring ``cancel_autolevel``. A no-op returning False
        when no ramp is running."""
        session = self._level_match_session
        if session is None:
            return False
        return await session.cancel()

    def level_match_snapshot(self) -> dict[str, Any]:
        """The current per-geometry locks + last level-match outcome (for
        ``/status`` surfacing). Empty until the first level match runs."""
        return {
            "locks": self.level_lock_store.snapshot(),
            "last": (
                self._last_level_match.snapshot()
                if self._last_level_match is not None
                else None
            ),
        }

    # ------------------------------------------------------------------
    # Snapshot.
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return session_snapshot(self)
