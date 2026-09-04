# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Measurement-session state machine: multi-position MMM averaging + verify.

The web handler opens a fresh measurement window for each sweep, so renderers
pause only while the speaker is actively measuring. Single-position runs are
N=1 — the same flow without NEEDS_NEXT_POSITION.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

import numpy as np

from jasper.audio_measurement import (
    analysis,
    calibration,
    deconv,
    quality,
    sweep,
)
from jasper.audio_measurement.calibration import CalibrationRecord
from jasper.audio_measurement.excitation import (
    AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
)
from jasper.audio_measurement.peq import PEQ
from jasper.audio_measurement.ramp import (
    LISTENING_POSITION_CAP_BUMP_DB,
    LISTENING_POSITION_CAP_CEIL_DB,
    RECOVERABLE_ERRORS,
    MeasurementRamp,
    RampState,
)

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
from .state_guard import SessionStateGuard
from .status import (
    describe_current_config as describe_current_config,
    parse_current_correction as parse_current_correction,
    session_snapshot,
)
from ..log_event import log_event


# Owned by the session layer so HTTP and browser entry paths cannot
# drift into different measurement semantics.
DEFAULT_ROOM_POSITION_COUNT = 6
ROOM_POSITION_COUNT_CHOICES = (1, 3, DEFAULT_ROOM_POSITION_COUNT)
DEFAULT_REPEAT_MAIN_POSITION = True

# Room's full-band ESS sits 6 dB below the shared level-check window: two JTS3
# UMIK-2 runs (2026-07-15) reached full scale in the shared [-20, -12] dBFS one.
ROOM_LEVEL_WINDOW_LOW_DBFS = -26.0
ROOM_LEVEL_WINDOW_HIGH_DBFS = -18.0

logger = logging.getLogger(__name__)


def _bundles_enabled() -> bool:
    """Default ON; opt-out via JASPER_CORRECTION_SAVE_BUNDLES=0."""
    return os.environ.get("JASPER_CORRECTION_SAVE_BUNDLES", "1").strip() != "0"


DBFS_FLOOR = acoustic_quality.DBFS_FLOOR
SNR_BANDS_HZ = acoustic_quality.SNR_BANDS_HZ


def _dbfs(value: float) -> float:
    return acoustic_quality.dbfs(value)


def _band_levels_dbfs(samples: np.ndarray, sample_rate: int) -> list[dict[str, Any]]:
    return acoustic_quality.band_levels_dbfs(samples, sample_rate)


def _verify_snr_quality_warning(
    estimated_snr_db: float | None,
) -> tuple[bool, str]:
    """Whether a verify capture's own SNR should gate its accept verdict.

    Returns ``(quality_warned, reason)``; ``reason`` is ``""`` when not warned.
    The boundary is :data:`acoustic_quality.SNR_WARN_DB`, read rather than
    restated so this gate and that module's own "low" tier cannot drift, and
    the comparison is strict (``<``) to match it. ``None`` warns: it is a real
    capture-time degradation, not the default — several production paths
    populate ``noise_floor_db`` ahead of any verify capture.
    """
    if estimated_snr_db is None:
        return True, (
            "verify capture's SNR could not be estimated "
            "(no noise floor recorded)"
        )
    if estimated_snr_db < acoustic_quality.SNR_WARN_DB:
        return True, (
            f"verify capture estimated_snr_db={estimated_snr_db:.1f} "
            f"< {acoustic_quality.SNR_WARN_DB:.0f} dB"
        )
    return False, ""


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
    """Refused because a transient sweep/analysis task would race it.

    The web layer maps this to HTTP 409 rather than 500.
    """


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "type": self.type,
            "payload": dict(self.payload),
        }


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
    amplitude_dbfs: float = AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS

    # No `peq_f_low` / `peq_f_high` here: the band has one reader,
    # `MeasurementSession.correction_band_hz` (#1797). Add strategy character to
    # `strategy.CORRECTION_STRATEGIES` instead.
    correction_strategy: str = strategy.DEFAULT_CORRECTION_STRATEGY_ID


# Stranded-upload watchdog. A sweep plus upload normally completes in seconds;
# 120 s is headroom. Independent of voice_daemon's measurement-window clear.
AWAITING_CAPTURE_TIMEOUT_SEC = 120.0

# States parked on an automatic browser upload with no user action in the loop.
# The user-paced needs_next_position / needs_repeat_capture are deliberately NOT
# guarded: repositioning can take minutes and both carry their own Cancel.
_CAPTURE_TIMEOUT_STATES = frozenset({
    SessionState.NEEDS_NOISE_CAPTURE,
    SessionState.AWAITING_CAPTURE,
    SessionState.AWAITING_REPEAT_CAPTURE,
    SessionState.AWAITING_VERIFY_CAPTURE,
})

# States reset() refuses: a fire-and-forget sweep/analysis task is running and
# would set the next state AFTER reset() sets IDLE. Every settled, parked or
# wedged state still resets, so the escape hatch keeps working.
_RESET_BUSY_STATES = frozenset({
    SessionState.PREPARING,
    SessionState.SWEEPING,
    SessionState.ANALYZING,
    SessionState.VERIFYING,
})


class MeasurementSession:
    """Multi-position measurement session; one per `POST /start`.

    Lives until the next `/start` replaces it or the daemon restarts.
    """

    def __init__(
        self,
        cfg: SessionConfig | None = None,
        *,
        total_positions: int = DEFAULT_ROOM_POSITION_COUNT,
        target_choice: str = strategy.DEFAULT_TARGET_PROFILE_ID,
        strategy_choice: str | None = None,
        mic_calibration: CalibrationRecord | None = None,
        input_device: dict[str, Any] | None = None,
        repeat_main_position: bool = DEFAULT_REPEAT_MAIN_POSITION,
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
        # Per-position smoothed magnitudes (dB on a log grid).
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

        self.measured_curve: CurveJSON | None = None
        self.target_curve: CurveJSON | None = None
        self.predicted_curve: CurveJSON | None = None
        # Pre-correction curve at the FIRST measured position: the matched
        # comparison basis for acceptance (the verify capture is taken there).
        self.position1_curve: CurveJSON | None = None
        self.verify_curve: CurveJSON | None = None
        self.verify_metrics: dict[str, float] | None = None
        # Measured before/after readout from the verify path, over the SAME
        # band as verify_metrics. None until a verify measurement lands.
        self.verify_before_after: dict[str, Any] | None = None
        # P4 acceptance verdict; None until a verify lands. A clear regression
        # auto-reverts only when the immediately following verify concurs.
        self.acceptance: dict[str, Any] | None = None
        self._verify_count = 0
        self._prior_clear_regression = False
        # Recorded when the automatic rollback completes, never predicted:
        # {"result": "ok"|"failed", "at": ts}. None = none has finished.
        self.auto_revert_outcome: dict[str, Any] | None = None
        self.design_report: dict[str, Any] | None = None

        self.peqs: list[PEQJSON] = []
        self.config_path: Path | None = None
        # The CamillaDSP config that was live immediately BEFORE apply().
        self.pre_apply_config_path: str | None = None
        self.pre_measurement_config_path: Path | None = None
        # Session-unique copy of CamillaDSP's running graph at Start; rollback
        # must load this, never the predecessor NAME (which may be rewritten).
        self.pre_measurement_restore_path: Path | None = None
        self.measurement_config_path: Path | None = None
        # Opaque Active-owned admission sampled at /start, revalidated at each
        # DSP-writer boundary. The session never interprets it.
        self.room_authority_binding: (
            tuple[bool | None, str | None, str | None] | None
        ) = None

        self.sweep_meta: sweep.SweepMeta | None = None
        self.sweep_wav_path: Path | None = None
        self.last_capture_path: Path | None = None

        self._autolevel_controller = AutolevelController(
            session_id=self.session_id,
        )
        self._autolevel_gate_obj: asyncio.Lock | None = None
        self._autolevel_reset_intent: object | None = None
        self._background_audio_task: asyncio.Task[Any] | None = None

        # P2 status-fed level match. The lock store is per-geometry, so a
        # near-field lock and a listening-position lock coexist.
        self.level_lock_store = LevelLockStore()
        self._last_level_match: LevelMatchOutcome | None = None
        # Per-run slot, not a permanent controller: `run_level_match` refuses
        # to start while it is occupied and clears it identity-guarded.
        self._level_match_session: LevelMatchSession | None = None
        self._level_match_task: asyncio.Task[Any] | None = None
        # Cleanup can fire from capture failure, reset and apply at once; serialize
        # so the lease releases once and a failed write stays retryable.
        self._level_restore_lock = asyncio.Lock()
        # The local browser learns its realized device only after `/start`.
        # This one-shot guard prevents a stale tab or later position from
        # changing capture identity after the run has been admitted.
        self._local_capture_setup_bound = False

        # capture_timeout_sec stays overridable for tests; <= 0 disables.
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

        # Optional client-reported room noise floor; saved into info.json.
        self.noise_floor_db: float | None = None

        # Active CamillaDSP config at `/start`, before the measurement baseline
        # is emitted. `/start` rejects graphs the carrier cannot preserve.
        self.current_correction_at_start: dict[str, Any] | None = None

        # Created lazily on first write.
        self.bundle_dir: Path = self.cfg.sessions_dir / self.session_id
        self.save_bundles: bool = _bundles_enabled()
        self.artifacts = SessionArtifacts(self)

        self._events: list[SessionEvent] = []
        self._event_seq = 0
        # Lazy-init: asyncio.Lock binds to the running loop at construction and
        # the session is constructed from sync HTTP-handler threads.
        self._lock_obj: asyncio.Lock | None = None

    @property
    def correction_band_hz(self) -> tuple[float, float]:
        """The band THIS session actually corrects, as ``(f_low, f_high)``.

        The single reader of the session's band, resolved fresh from
        `strategy.CORRECTION_STRATEGIES` so that table stays the one owner of
        strategy character (#1797).
        """
        strat = strategy.resolve_correction_strategy(self.strategy_choice)
        return (strat.f_low_hz, strat.f_high_hz)

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
    def autolevel_run_in_progress(self) -> bool:
        """Whether local level matching still owns or may write audio state."""
        return self._autolevel_controller.run_in_progress

    @property
    def _autolevel_gate(self) -> asyncio.Lock:
        if self._autolevel_gate_obj is None:
            self._autolevel_gate_obj = asyncio.Lock()
        return self._autolevel_gate_obj

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

    @property
    def local_capture_setup_bound(self) -> bool:
        """Whether the one-shot local browser identity has been accepted."""
        return self._local_capture_setup_bound


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

    def events_snapshot(self) -> list[dict[str, Any]]:
        """Return point-in-time session events for the live status surface."""

        return [event.to_dict() for event in self._events]

    def _cancel_capture_timeout(self) -> None:
        self._state_guard.cancel_capture_timeout()

    def suspend_capture_timeout(self) -> None:
        """Pause the upload watchdog during human-paced capture setup."""
        self._state_guard.cancel_capture_timeout()

    def resume_capture_timeout(self) -> None:
        """Restore the watchdog for the session's current capture state."""
        self._state_guard.on_transition(self.state)

    async def resume_capture_timeout_on_loop(self) -> None:
        """Re-arm the watchdog on the session's owning asyncio loop."""
        self.resume_capture_timeout()

    async def _set_state(self, state: SessionState, **extra: Any) -> None:
        prev = self.state
        self.state = state
        # Cancel any pending timer; re-arm only for automatic-upload states.
        self._state_guard.on_transition(state)
        payload = {"state": state.value, "prev": prev.value, **extra}
        self._emit("state", payload)
        logger.info(
            "session %s: %s → %s %s",
            self.session_id, prev.value, state.value,
            extra if extra else "",
        )
        # Best-effort: a bundle write failure must not break a transition.
        try:
            self._write_info_json()
        except Exception:  # noqa: BLE001
            logger.exception(
                "bundle info.json write failed (state=%s)", state.value,
            )


    def _bundle_relative_path(self, path: Path) -> str | None:
        return self.artifacts.bundle_relative_path(path)

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
        """Where a per-position WAV should be written."""
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

    async def run_background_audio_operation(
        self,
        operation: Callable[[], Awaitable[None]],
    ) -> None:
        """Run one cancellable sweep operation in an identity-guarded slot."""
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("background audio operation has no asyncio task")
        async with self._autolevel_gate:
            if self._autolevel_reset_intent is not None:
                raise SessionBusyError("room-correction reset is in progress")
            current = self._background_audio_task
            if current is not None and not current.done():
                raise SessionBusyError("measurement audio is already running")
            self._background_audio_task = task
        try:
            await operation()
        finally:
            async with self._autolevel_gate:
                if self._background_audio_task is task:
                    self._background_audio_task = None

    async def stop_background_audio_for_reset(self) -> bool:
        """Cancel and reap every Room audio owner before graph rollback."""
        current_task = asyncio.current_task()
        async with self._autolevel_gate:
            background_task = self._background_audio_task
            level_task = self._level_match_task
            level_session = self._level_match_session
            tasks = [
                task
                for task in dict.fromkeys((background_task, level_task))
                if task is not None and not task.done()
            ]
        if current_task in tasks:
            raise RuntimeError("background audio operation cannot stop itself")
        if not tasks:
            return False

        # A live ramp owns volume writes: ask its controller to exit through the
        # click-free fade and exact listening-volume restore, then await it.
        level_stopping_gracefully = False
        if (
            level_task is not None
            and not level_task.done()
            and level_session is not None
        ):
            level_stopping_gracefully = await level_session.cancel()

        for task in tasks:
            if task is not level_task or not level_stopping_gracefully:
                task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            if self._state_guard.is_reset_busy(self.state):
                self.error = "measurement stopped"
                await self._set_state(
                    SessionState.FAILED,
                    reason="emergency_stop",
                )
        return True

    async def _fail(self, message: str) -> None:
        self._cancel_capture_timeout()
        owned_intent: object | None = None
        async with self._autolevel_gate:
            if self._autolevel_reset_intent is None:
                owned_intent = object()
                self._autolevel_reset_intent = owned_intent
            reservation = self._autolevel_controller.reservation_token
        try:
            if self.autolevel_run_in_progress:
                await self.cancel_autolevel_and_wait()
            if reservation is not None:
                await self._autolevel_controller.wait_for_run_reservation_release(
                    reservation,
                )
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
            # autolevel level; the web handlers never run on this path.
            await self._restore_listening_volume_if_ramped()
        finally:
            if owned_intent is not None:
                await self.end_autolevel_reset(owned_intent)

    async def _restore_listening_volume_if_ramped(self) -> None:
        """Restore main_volume to the pre-autolevel listening level.

        Best-effort, idempotent and lock-free, for the endings the web
        apply/reset handlers never see (watchdog FAILED, verify VERIFIED).
        """
        await self._autolevel_controller.restore_listening_volume_if_ramped()

    def _ensure_sweep_cache(self) -> tuple[Path, sweep.SweepMeta]:
        """Generate or reuse the cached sweep WAV (deterministic per parameters)."""
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
        # Bound the noise array up front: the /upload-noise body is capped only
        # by the 32 MB HTTP limit and the math below would spike the 1 GB Pi.
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
            # #1838: this dict is persisted and re-read later, so it carries the
            # estimator marker itself. Absent = pre-#1838 scale; do not diff.
            "band_snr_scale": acoustic_quality.BAND_SNR_SCALE,
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
        return acoustic_quality.capture_band_snr(
            captured_wav_path,
            noise_report,
        )

    def _repeatability_from_arrays(
        self,
        first: np.ndarray,
        repeat: np.ndarray,
        freqs_hz: np.ndarray,
    ) -> dict[str, Any]:
        return acoustic_quality.repeatability_from_arrays(
            first,
            repeat,
            freqs_hz,
            peq_f_high=self.correction_band_hz[1],
        )

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

        Returns ``(log_freqs, smoothed_db, capture_quality, direct_arrival,
        replay_artifacts)``.
        """

        def _log_quality_issue(issue: quality.QualityIssue) -> None:
            logger.warning(
                "capture_quality session=%s code=%s severity=%s detail=%s",
                self.session_id, issue.code, issue.severity, issue.message,
            )

        result = acoustic_quality.analyze_capture(
            captured_wav_path,
            sweep_meta=self.sweep_meta,
            expected_sample_rate=self.cfg.sample_rate,
            mic_calibration=self.mic_calibration,
            input_device=self.input_device,
            normalize_band_hz=ANALYSIS_NORMALIZE_BAND_HZ,
            on_quality_issue=_log_quality_issue,
        )
        replay_artifact_info = self._write_capture_replay_artifacts(
            captured_wav_path,
            capture_kind=capture_kind,
            position_index=position_index,
            ir=result.impulse_response,
            raw_freqs_hz=result.raw_freqs_hz,
            raw_magnitude_db=result.raw_magnitude_db,
            smoothed_magnitude_db=result.smoothed_magnitude_db,
            log_freqs_hz=result.log_freqs_hz,
            log_magnitude_db=result.log_magnitude_db,
            direct_arrival=result.direct_arrival,
        )
        return (
            result.log_freqs_hz,
            result.log_magnitude_db,
            result.capture_quality,
            result.direct_arrival,
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
            # #2058 SF6: read SNR_WARN_DB rather than restating 20.0, so this
            # issue and acoustic_quality's own "low" tier cannot drift apart.
            if estimated_snr_db < acoustic_quality.SNR_WARN_DB:
                issues = list(out.get("issues") or [])
                issues.append({
                    "code": "capture_snr_low",
                    "severity": "warn",
                    "message": (
                        f"capture is less than {acoustic_quality.SNR_WARN_DB:.0f} "
                        "dB above the measured pre-sweep noise floor"
                    ),
                    "details": {
                        "estimated_snr_db": round(estimated_snr_db, 2),
                        "threshold_db": acoustic_quality.SNR_WARN_DB,
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

        Both deviations are taken over the SAME band as `verify_metrics`
        (``[50, self.correction_band_hz[1]]``), never the design report's
        predicted "before". Returns None if the pre-correction curve is
        unavailable.
        """
        if self.measured_curve is None:
            return None
        pre_freqs = np.asarray(self.measured_curve.freqs_hz, dtype=np.float64)
        pre_mag = np.asarray(self.measured_curve.magnitude_db, dtype=np.float64)
        if pre_freqs.size == 0 or pre_mag.size != pre_freqs.size:
            return None
        before_on_grid = np.interp(verify_freqs, pre_freqs, pre_mag)
        return analysis.before_after_delta(
            verify_freqs,
            before_on_grid,
            verify_mag_db,
            target_db,
            f_high=self.correction_band_hz[1],
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

        It also folds in a disclosure (#2058): this verify capture's own SNR
        (low or unestimable — see :func:`_verify_snr_quality_warning`;
        ``self.verify_quality`` is populated by the caller before this method
        runs) rides along on the returned dict as ``verify_quality_warned`` /
        ``verify_quality_reason``. It never touches ``result.verdict`` — the
        pure evaluator's verdict is the one judge; this is information for
        the household, not a second verdict overruling the first.

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

            before_on_grid = np.interp(verify_freqs, basis_freqs, basis_mag)

            self._verify_count += 1
            result = acceptance.evaluate_acceptance(
                freqs=verify_freqs,
                before_db=before_on_grid,
                verify_db=verify_mag_db,
                target_db=target_db,
                f_high=self.correction_band_hz[1],
                basis=basis,
                verify_index=self._verify_count,
                prior_clear_regression=self._prior_clear_regression,
            )
            # Verify-capture quality rides along as disclosure, never a gate
            # (#2058). Deliberately not acoustic_quality's aggregate level: that
            # is "warn" on nearly every session that skips a mic calibration,
            # while a capture-local noise floor does not cancel between before
            # and verify and so bears on the extracted curve shape itself.
            verify_snr_db = (
                self.verify_quality.get("estimated_snr_db")
                if isinstance(self.verify_quality, dict)
                else None
            )
            if not (
                isinstance(verify_snr_db, (int, float))
                and not isinstance(verify_snr_db, bool)
            ):
                verify_snr_db = None
            quality_warned, quality_reason = _verify_snr_quality_warning(
                verify_snr_db,
            )
            # Strict adjacency: a clean verify clears the flag, so a later
            # regression must earn its own confirmatory re-measure. Latched
            # semantics would compound the single-sweep false-flag rate.
            self._prior_clear_regression = result.clear_regression
            result_dict = result.to_dict()
            result_dict["verify_quality_warned"] = quality_warned
            result_dict["verify_quality_reason"] = quality_reason
            return result_dict
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
            correction_band_hz=self.correction_band_hz,
        )


    async def begin_noise_capture(self) -> None:
        """Ask the browser to record pre-sweep room noise.

        Direct test/legacy callers may still call `prepare_and_play_sweep()`
        from IDLE.
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

    async def bind_local_capture_setup(
        self,
        *,
        mic_calibration: CalibrationRecord | None,
        input_device: dict[str, Any],
    ) -> dict[str, Any]:
        """Bind the realized local-browser input before its first upload.

        Only the parked pre-sweep state is mutable; capture setup continues
        through its own versioned binding path.
        """
        report = browser_audio.assess_browser_audio_path(
            input_device=input_device,
            expected_sample_rate=self.cfg.sample_rate,
            has_mic_calibration=mic_calibration is not None,
        ).to_dict()
        if report.get("failed") is True:
            raise ValueError(
                report.get("summary")
                or "browser audio path is not safe for measurement"
            )

        async with self._lock:
            if self.state != SessionState.NEEDS_NOISE_CAPTURE:
                raise RuntimeError(
                    "cannot bind local capture setup from state "
                    f"{self.state.value}"
                )
            if self.current_position != 0 or self.position_magnitudes:
                raise RuntimeError(
                    "local capture setup cannot change after measurement begins"
                )
            if self._local_capture_setup_bound:
                current_calibration_id = getattr(
                    self.mic_calibration, "calibration_id", None
                )
                requested_calibration_id = getattr(
                    mic_calibration, "calibration_id", None
                )
                if (
                    self.input_device == dict(input_device)
                    and current_calibration_id == requested_calibration_id
                ):
                    return dict(self.browser_audio_report)
                raise RuntimeError(
                    "local capture setup cannot change after it is bound"
                )
            self.mic_calibration = mic_calibration
            self.input_device = dict(input_device)
            self.browser_audio_report = report
            self._local_capture_setup_bound = True
            self._emit(
                "local_capture_setup",
                {
                    "calibrated": mic_calibration is not None,
                    "browser_audio_level": str(report.get("level") or ""),
                },
            )
            try:
                self._write_info_json()
            except Exception:  # noqa: BLE001
                logger.exception("bundle info.json write failed (local capture setup)")
        return report

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

    async def _play_prepared_sweep(
        self,
        play_sweep_async: Callable[..., Awaitable[Any]],
        *,
        capture_kind: str,
        position_index: int | None,
        position_payload: dict[str, int] | None,
        awaiting_state: SessionState,
        playback_error: str,
        alsa_device: str | None = None,
        runtime_probe_async: (
            Callable[[], Awaitable[dict[str, Any] | None]] | None
        ) = None,
    ) -> None:
        """Play a sweep after the caller validates and enters its prepare state.

        ``position_payload`` is optional: all sweep events expose duration,
        while measurement and repeat also expose position metadata.
        """
        await self._record_runtime_snapshot(
            f"{capture_kind}_prepare",
            capture_kind=capture_kind,
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
            sweeping_payload: dict[str, Any] = {
                "duration_s": meta.duration_s,
                **(position_payload or {}),
            }
            await self._set_state(SessionState.SWEEPING, **sweeping_payload)

        try:
            kwargs = {"alsa_device": alsa_device} if alsa_device else {}
            await self._record_runtime_snapshot(
                f"{capture_kind}_sweep_start",
                capture_kind=capture_kind,
                position_index=position_index,
                runtime_probe_async=runtime_probe_async,
            )
            await play_sweep_async(str(sweep_wav), **kwargs)
            await self._record_runtime_snapshot(
                f"{capture_kind}_sweep_complete",
                capture_kind=capture_kind,
                position_index=position_index,
                runtime_probe_async=runtime_probe_async,
            )
        except Exception as e:  # noqa: BLE001
            await self._record_runtime_snapshot(
                f"{capture_kind}_sweep_failed",
                capture_kind=capture_kind,
                position_index=position_index,
                runtime_probe_async=runtime_probe_async,
            )
            async with self._lock:
                await self._fail(f"{playback_error}: {e}")
            raise

        async with self._lock:
            await self._set_state(
                awaiting_state,
                **(position_payload or {}),
            )

    async def prepare_and_play_sweep(
        self,
        play_sweep_async: Callable[..., Awaitable[Any]],
        *,
        alsa_device: str | None = None,
        runtime_probe_async: (
            Callable[[], Awaitable[dict[str, Any] | None]] | None
        ) = None,
    ) -> None:
        """Single sweep, for position[i] and for the single-position path.

        Flow: PREPARING -> SWEEPING -> AWAITING_CAPTURE. The caller owns the
        measurement window.
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

        await self._play_prepared_sweep(
            play_sweep_async,
            capture_kind="measurement",
            position_index=position_index,
            position_payload={
                "position": position_index,
                "total_positions": self.total_positions,
            },
            awaiting_state=SessionState.AWAITING_CAPTURE,
            playback_error="sweep playback failed",
            alsa_device=alsa_device,
            runtime_probe_async=runtime_probe_async,
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

        Stored separately so bundle recompute does not mistake it for another
        listening position.
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

        await self._play_prepared_sweep(
            play_sweep_async,
            capture_kind="repeat",
            position_index=position_index,
            position_payload={
                "position": position_index,
                "total_positions": self.total_positions,
            },
            awaiting_state=SessionState.AWAITING_REPEAT_CAPTURE,
            playback_error="repeat sweep playback failed",
            alsa_device=alsa_device,
            runtime_probe_async=runtime_probe_async,
        )

    async def on_capture_uploaded(
        self, captured_wav_path: Path,
    ) -> None:
        """Position-N capture arrived: deconvolve, smooth and store."""
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

        # Deconvolution, smoothing and PEQ design are multi-second NumPy: run
        # them on a worker thread so they do not monopolize the shared
        # correction event loop. Safe without extra locking because ANALYZING is
        # reset-busy with the capture watchdog disarmed.
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

        await self._advance_position_or_design()

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

        await self._advance_position_or_design()

    async def _advance_position_or_design(self) -> None:
        """Advance to the next seat or finish the shared correction design."""
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
        """Spatial-average positions, look up the target, design the PEQs."""
        if not self.position_magnitudes or self.position_freqs is None:
            raise RuntimeError(
                "no position data — run capture first"
            )

        averaged_db = analysis.spatial_average_db(self.position_magnitudes)
        log_freqs = self.position_freqs
        # The room designer only READS the bass-management corner (the speaker
        # layer owns it), so it can refuse to boost inside the crossover region.
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
        # Retain position 1 separately so the P4 verify compares against the
        # SAME geometry it re-measures at, not the multi-seat spatial average.
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


    async def apply(
        self,
        camilla_set_config: Callable[[str], Awaitable[bool]],
        camilla_get_config: Callable[[], Awaitable[str | None]],
        *,
        prepare_guard: Callable[[], Awaitable[Mapping[str, Any]]] | None = None,
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
            from jasper.sound.profile import build_sound_filters, load_profile
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"YAML emit failed: {e}")
            raise

        async def _prepare_config() -> dict[str, Any]:
            # apply_dsp_config invokes prepare only after acquiring the shared
            # DSP-writer lock, so no legal writer can change Layer A between the
            # decision and carrier re-emission.
            if prepare_guard is None:
                raise RuntimeError(
                    "room-correction bass authority evidence is missing"
                )
            guarded_summary = await prepare_guard()
            if not isinstance(guarded_summary, Mapping):
                raise RuntimeError(
                    "room-correction bass authority evidence is invalid"
                )
            bass_profile_summary: Mapping[str, Any] = guarded_summary
            profile = load_profile()
            from jasper.fanin_coupling import coupling_capture_kwargs_from_env
            from jasper.sound.graph_carrier import carrier_for_loaded_config

            prior_config_path = await camilla_get_config()
            if not prior_config_path:
                raise RuntimeError(
                    "CamillaDSP did not report a loaded config path"
                )
            # Remember the pre-swap graph so a confirmed-regression auto-revert
            # can restore it via the existing reset() path.
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

            assert_correction_graph_safe(
                result.yaml,
                bass_profile_summary=bass_profile_summary,
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
        """One-position re-measurement after Apply.

        Lands in self.verify_curve / self.verify_metrics plus the measured
        before/after readout in self.verify_before_after.
        """
        async with self._lock:
            if self.state != SessionState.APPLIED and self.state != SessionState.VERIFIED:
                raise RuntimeError(
                    f"cannot verify from state {self.state.value}"
                )
            await self._set_state(SessionState.VERIFYING)

        await self._play_prepared_sweep(
            play_sweep_async,
            capture_kind="verify",
            position_index=None,
            position_payload=None,
            awaiting_state=SessionState.AWAITING_VERIFY_CAPTURE,
            playback_error="verify sweep playback failed",
            alsa_device=alsa_device,
            runtime_probe_async=runtime_probe_async,
        )

    async def on_verify_capture_uploaded(
        self, captured_wav_path: Path,
    ) -> None:
        """Verify capture arrived: store the curve, metrics and before/after."""
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
        # 50 Hz low edge, not the PEQ design band's 20-25 Hz: below ~50 Hz the
        # iPhone mic's built-in 24 dB/octave HPF dominates the capture, so those
        # bins are a mic artifact in a deviation readout. Design still reaches
        # 20 Hz, where the capture informs a filter but not a clean readout.
        metrics = analysis.deviation_metrics(
            log_mag, target_db, log_freqs,
            f_high=self.correction_band_hz[1],
        )

        self.verify_curve = CurveJSON(
            freqs_hz=log_freqs.tolist(),
            magnitude_db=log_mag.tolist(),
        )
        self.verify_metrics = metrics
        self.verify_before_after = self._compute_verify_before_after(
            log_freqs, log_mag, target_db,
        )

        # Quality BEFORE verdict (#2058): the acceptance verdict below consults
        # self.verify_quality, so both reports must be populated first.
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

        # Pure accept/surface/revert verdict. On a confirmed regression the web
        # layer performs the rollback; the session never writes CamillaDSP.
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
        # listening level here too if autolevel ramped it.
        await self._restore_listening_volume_if_ramped()

    @property
    def acceptance_verdict(self) -> str | None:
        """The current P4 verdict string, or None before a verify lands."""
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

        Fires only on the ``revert`` verdict (a second concordant verify); every
        other verdict is a no-op returning False. Rides the existing reset()
        reversal. ``target_config_path`` is the graph to restore, defaulting to
        the pre-apply config and then to reset()'s base graph. Returns True only
        when the rollback completed; the outcome is recorded on
        ``self.auto_revert_outcome`` when it is known, never predicted.
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
        # try/finally, not try/except, so a raising reset() still records a
        # truthful "failed" outcome while the exception propagates untouched.
        # reset() has two non-raising terminal shapes: IDLE (rolled back) or
        # _fail -> FAILED. Only the first is a performed rollback.
        ok = False
        try:
            await self.reset(camilla_set_config, target_config_path=target)
            ok = self.state == SessionState.IDLE
        finally:
            self._record_auto_revert_outcome("ok" if ok else "failed")
        return ok

    def _record_auto_revert_outcome(self, result: str) -> None:
        """Record the completed rollback outcome and rewrite the evidence.

        Runs inside auto_revert()'s finally-block, so it must never raise.
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


    async def run_autolevel(
        self,
        *,
        reservation_token: object | None = None,
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
        """Auto-level CamillaDSP main_volume against a continuous tone.

        ``end_db`` defaults to ``clamp(original_main_volume_db + end_db_bump,
        [end_db_absolute_min, end_db_absolute_max])``: +6 dB over the
        household's listening level, clamped to [-20, -6] dB. With the -12 dBFS
        tone that puts worst-case dongle output at -18 dBFS. Exits LOCKED,
        MAXED_OUT (no measurement lock minted) or CANCELLED.
        """
        await self._autolevel_controller.run(
            reservation_token=reservation_token,
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
        """Stop ramping and lock at the current main_volume."""
        return await self._autolevel_controller.lock()

    async def reserve_autolevel_run(self) -> object | None:
        """Atomically validate phase and reserve one exact local ramp."""
        retryable = {
            AutolevelStatus.IDLE,
            AutolevelStatus.CANCELLED,
            AutolevelStatus.ERROR,
            AutolevelStatus.MAXED_OUT,
        }
        async with self._autolevel_gate:
            if (
                self._autolevel_reset_intent is not None
                or self.state != SessionState.NEEDS_NOISE_CAPTURE
                or not self.local_capture_setup_bound
                or self.autolevel.status not in retryable
            ):
                return None
            return await self._autolevel_controller.reserve_run()

    async def release_autolevel_run_reservation(self, token: object) -> bool:
        """Release one exact adapter slot after outer orchestration exits."""
        async with self._autolevel_gate:
            return await self._autolevel_controller.release_run_reservation(token)

    async def begin_autolevel_reset(self) -> object:
        """Block new Room audio and quiesce the active local ramp generation."""
        intent = object()
        async with self._autolevel_gate:
            if self._autolevel_reset_intent is not None:
                raise SessionBusyError("room-correction reset is already in progress")
            self._autolevel_reset_intent = intent
            reservation = self._autolevel_controller.reservation_token
        reset_ready = False
        try:
            if self.autolevel_run_in_progress:
                await self.cancel_autolevel_and_wait(timeout_s=35.0)
            if reservation is not None:
                await self._autolevel_controller.wait_for_run_reservation_release(
                    reservation,
                    timeout_s=35.0,
                )
            reset_ready = True
            return intent
        finally:
            if not reset_ready:
                # The caller has no token to release when begin itself fails;
                # roll back so a timeout cannot wedge every future Stop.
                await asyncio.shield(self.end_autolevel_reset(intent))

    async def end_autolevel_reset(self, intent: object) -> bool:
        """Release only the reset intent created by :meth:`begin_autolevel_reset`."""
        async with self._autolevel_gate:
            if intent is not self._autolevel_reset_intent:
                return False
            self._autolevel_reset_intent = None
            return True

    async def cancel_autolevel(self) -> bool:
        """Abort the running autolevel task and restore the original volume."""
        return await self._autolevel_controller.cancel()

    async def cancel_autolevel_and_wait(self, *, timeout_s: float = 5.0) -> bool:
        """Cancel a running ramp and await its listening-volume restore."""
        return await self._autolevel_controller.cancel_and_wait(timeout_s=timeout_s)


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
        """Status-fed, settle-based level match for one mic geometry.

        ``read_status`` is the status reader, injected so this method
        never imports the wizard's HTTP client; in production it must be a cached
        background-poller snapshot, never a blocking per-call HTTP GET. A
        terminal LOCKED stores a per-geometry lock in ``level_lock_store``.
        """
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("level match has no asyncio task")
        # Single-flight: one level match at a time per session. An overlapping
        # run would orphan the first's live ramp from its Lock/Cancel seam.
        async with self._autolevel_gate:
            if self._autolevel_reset_intent is not None:
                raise SessionBusyError("room-correction reset is in progress")
            if self._level_match_session is not None:
                raise RuntimeError("level match already in progress")
            prior = self._last_level_match
            if (
                prior is not None
                and prior.ramp.state is RampState.LOCKED
                and prior.ramp.restored is not True
            ):
                raise RuntimeError(
                    "measurement level is already locked; finish or cancel the "
                    "current measurement before checking it again"
                )
            # Retain the adapter and its owner task so Lock/Cancel/Reset can
            # reach it without racing a replacement.
            session = LevelMatchSession(
                session_id=self.session_id,
                store=self.level_lock_store,
                # A room sweep has substantial deconvolution/averaging gain, so
                # at a stable low cap keep explicitly degraded evidence.
                config=MeasurementRamp.from_env(
                    allow_bounded_low_level=True,
                    cap_bump_db=LISTENING_POSITION_CAP_BUMP_DB,
                    cap_ceil_db=LISTENING_POSITION_CAP_CEIL_DB,
                    window_low_dbfs=ROOM_LEVEL_WINDOW_LOW_DBFS,
                    window_high_dbfs=ROOM_LEVEL_WINDOW_HIGH_DBFS,
                ),
            )
            self._level_match_session = session
            self._level_match_task = task
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
            async with self._autolevel_gate:
                if self._level_match_session is session:
                    self._level_match_session = None
                if self._level_match_task is task:
                    self._level_match_task = None
        self._last_level_match = outcome
        if outcome.locked:
            # A level check owns the loud target only while its tone window is
            # active: return to the listening level before the window closes.
            restored = await self.restore_level_match_volume(set_main_volume_db)
            if not restored:
                raise RuntimeError(
                    "measurement level locked, but the listening volume could "
                    "not be restored"
                )
        return outcome

    async def restore_level_match_volume(
        self,
        set_main_volume_db: Callable[[float], Awaitable[Any]],
    ) -> bool:
        """Restore the exact pre-ramp listening volume once.

        MAXED_OUT / error / cancel paths are restored by the kernel itself.
        """
        async with self._level_restore_lock:
            outcome = self._last_level_match
            if outcome is None or outcome.ramp.state is not RampState.LOCKED:
                return False
            ramp = outcome.ramp
            if ramp.restored or ramp.original_main_volume_db is None:
                return False
            applied = await set_main_volume_db(float(ramp.original_main_volume_db))
            if applied is False:
                log_event(
                    logger,
                    "level_match_volume_restore_failed",
                    level=logging.ERROR,
                    session=self.session_id,
                    geometry=outcome.geometry,
                    to_db=f"{ramp.original_main_volume_db:.1f}",
                )
                return False
            ramp.restored = True
            log_event(
                logger,
                "level_match_volume_restored",
                session=self.session_id,
                geometry=outcome.geometry,
                to_db=f"{ramp.original_main_volume_db:.1f}",
            )
            return True

    async def ensure_level_match_volume(
        self,
        set_main_volume_db: Callable[[float], Awaitable[Any]],
    ) -> bool:
        """Reassert the saved lock immediately before an acoustic sweep.

        The physical remote is external, so a cached ``locked`` flag alone is
        not permission to play.
        """
        async with self._level_restore_lock:
            outcome = self._last_level_match
            if outcome is None or outcome.ramp.state is not RampState.LOCKED:
                return False
            ramp = outcome.ramp
            if ramp.locked_main_volume_db is None:
                return False
            # Already asserted for this sweep window.
            if ramp.restored is not True:
                return True
            applied = await set_main_volume_db(float(ramp.locked_main_volume_db))
            if applied is False:
                log_event(
                    logger,
                    "level_match_volume_reassert_failed",
                    level=logging.ERROR,
                    session=self.session_id,
                    geometry=outcome.geometry,
                    to_db=f"{ramp.locked_main_volume_db:.1f}",
                )
                return False
            ramp.restored = False
            log_event(
                logger,
                "level_match_volume_reasserted",
                session=self.session_id,
                geometry=outcome.geometry,
                to_db=f"{ramp.locked_main_volume_db:.1f}",
            )
            return True

    async def lock_level_match(self) -> bool:
        """Manual lock: freeze the running ramp at its current level."""
        session = self._level_match_session
        if session is None:
            return False
        return await session.lock_now()

    async def cancel_level_match(self) -> bool:
        """Abort a running level match; the kernel owns the volume restore."""
        session = self._level_match_session
        if session is None:
            return False
        return await session.cancel()

    def level_match_snapshot(self) -> dict[str, Any]:
        """Per-geometry locks and the last level-match outcome, for ``/status``."""
        return {
            "running": self._level_match_session is not None,
            "locks": self.level_lock_store.snapshot(),
            "last": (
                self._last_level_match.snapshot()
                if self._last_level_match is not None
                else None
            ),
        }


    def snapshot(self) -> dict[str, Any]:
        return session_snapshot(self)
