"""Measurement-session state machine.

Phase 2: multi-position MMM averaging + verify pass.

The session owns a long-running async task that holds the
measurement window open across all 5 sweeps so the renderers don't
pause/restart between positions (which would cost ~3-5 s per
position change). The HTTP handler `POST /start` kicks off the
session; subsequent `POST /next-position` and `POST /upload-capture`
calls advance the state machine; `POST /apply` closes the window
and applies the correction; `POST /verify` opens a fresh window for
a post-correction re-measurement; `POST /reset` rolls back to the
base config.

State transitions (with N = total_positions):

    IDLE → PREPARING → SWEEPING → AWAITING_CAPTURE
         → (on_capture_uploaded for position 0 ... N-2)
         → NEEDS_NEXT_POSITION
         → SWEEPING (position 1)
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
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np

from . import analysis, deconv, peq, sweep, target
from .camilla_yaml import emit_correction_config
from .peq import PEQ

logger = logging.getLogger(__name__)


class SessionState(Enum):
    IDLE = "idle"
    PREPARING = "preparing"
    SWEEPING = "sweeping"
    AWAITING_CAPTURE = "awaiting_capture"
    NEEDS_NEXT_POSITION = "needs_next_position"
    ANALYZING = "analyzing"
    READY = "ready"
    APPLIED = "applied"
    VERIFYING = "verifying"
    AWAITING_VERIFY_CAPTURE = "awaiting_verify_capture"
    VERIFIED = "verified"
    FAILED = "failed"


# Targets the user can pick from a dropdown. Maps name → warmth
# parameter (target.house_curve). 'flat' is a special case.
TARGET_CHOICES = {
    "flat": None,           # = target.flat_target
    "neutral": 0.0,         # = flat (interpolant midpoint)
    "warm": 0.7,            # = mostly Harman
    "bright": -0.3,         # = inverse Harman tilt
}


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
    config_dir: Path = Path("/var/lib/camilladsp/configs")
    base_config_path: Path = Path("/etc/camilladsp/v1.yml")

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
    ) -> None:
        self.cfg = cfg or SessionConfig()
        self.session_id = uuid.uuid4().hex[:12]
        self.state = SessionState.IDLE
        self.started_at = time.time()
        self.updated_at = self.started_at
        self.error: str | None = None

        self.total_positions = max(1, int(total_positions))
        self.current_position = 0
        self.target_choice = (
            target_choice if target_choice in TARGET_CHOICES else "flat"
        )
        # Per-position smoothed magnitude responses (dB on log grid).
        # Spatial-averaged at end of multi-position flow.
        self.position_magnitudes: list[np.ndarray] = []
        self.position_freqs: np.ndarray | None = None  # log grid

        # Output curves for the chart.
        self.measured_curve: CurveJSON | None = None
        self.target_curve: CurveJSON | None = None
        self.predicted_curve: CurveJSON | None = None
        self.verify_curve: CurveJSON | None = None
        self.verify_metrics: dict[str, float] | None = None

        self.peqs: list[PEQJSON] = []
        self.config_path: Path | None = None

        # Sweep cache.
        self.sweep_meta: sweep.SweepMeta | None = None
        self.sweep_wav_path: Path | None = None
        self.last_capture_path: Path | None = None

        # Events / SSE.
        self._events: list[SessionEvent] = []
        self._event_seq = 0
        self._lock = asyncio.Lock()

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

    async def _set_state(self, state: SessionState, **extra: Any) -> None:
        prev = self.state
        self.state = state
        payload = {"state": state.value, "prev": prev.value, **extra}
        self._emit("state", payload)
        logger.info(
            "session %s: %s → %s %s",
            self.session_id, prev.value, state.value,
            extra if extra else "",
        )

    async def _fail(self, message: str) -> None:
        self.error = message
        self.state = SessionState.FAILED
        self._emit("error", {"message": message})
        logger.error("session %s failed: %s", self.session_id, message)

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

    def _smooth_capture(
        self, captured_wav_path: Path,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Read capture, deconvolve, smooth, log-resample. Returns
        (log_freqs, smoothed_db) — used both per-position and for
        the verify pass."""
        if self.sweep_meta is None:
            raise RuntimeError(
                "no sweep_meta — flow ordering bug (call _ensure_sweep_cache first)"
            )

        captured, sr = sweep.read_wav_mono(captured_wav_path)
        if sr != self.cfg.sample_rate:
            raise ValueError(
                f"captured sample rate {sr} != expected "
                f"{self.cfg.sample_rate}; the iOS Safari verify step "
                f"should have caught this"
            )
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
        freqs, mag_db = deconv.magnitude_response(ir, self.cfg.sample_rate)
        smoothed = analysis.smooth_fractional_octave(freqs, mag_db, fraction=48)
        log_freqs, log_mag = analysis.resample_log(freqs, smoothed)
        log_mag = analysis.normalize_to_band(log_freqs, log_mag)
        return log_freqs, log_mag

    def _design_target(self, freqs: np.ndarray) -> np.ndarray:
        """Resolve target_choice → dB target curve on `freqs`."""
        if self.target_choice == "flat":
            return target.flat_target(freqs)
        warmth = TARGET_CHOICES.get(self.target_choice, 0.0)
        if warmth is None:  # 'flat'
            return target.flat_target(freqs)
        return target.house_curve(freqs, warmth=warmth)

    # ------------------------------------------------------------------
    # Phase 1 / Phase 2 measurement flow.
    # ------------------------------------------------------------------

    async def prepare_and_play_sweep(
        self,
        play_sweep_async: Callable[..., Awaitable[Any]],
        *,
        alsa_device: str | None = None,
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
            await play_sweep_async(str(sweep_wav), **kwargs)
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"sweep playback failed: {e}")
            raise

        async with self._lock:
            await self._set_state(
                SessionState.AWAITING_CAPTURE,
                position=self.current_position,
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

        try:
            log_freqs, log_mag = self._smooth_capture(captured_wav_path)
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"analysis failed: {e}")
            raise

        if self.position_freqs is None:
            self.position_freqs = log_freqs
        self.position_magnitudes.append(log_mag)
        self.current_position += 1

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
            self._run_design_from_positions()
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"PEQ design failed: {e}")
            raise

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
        target_db = self._design_target(log_freqs)

        peqs = peq.design_peq(
            averaged_db, target_db, log_freqs,
            f_low=self.cfg.peq_f_low,
            f_high=self.cfg.peq_f_high,
            max_filters=self.cfg.peq_max_filters,
            max_cut_db=self.cfg.peq_max_cut_db,
            max_boost_db=self.cfg.peq_max_boost_db,
            cuts_only=self.cfg.peq_cuts_only,
            flatness_target_db=self.cfg.peq_flatness_target_db,
        )
        predicted_shift = peq.predicted_response(peqs, log_freqs)
        predicted_curve_db = averaged_db + predicted_shift

        self.measured_curve = CurveJSON(
            freqs_hz=log_freqs.tolist(),
            magnitude_db=averaged_db.tolist(),
        )
        self.target_curve = CurveJSON(
            freqs_hz=log_freqs.tolist(),
            magnitude_db=target_db.tolist(),
        )
        self.predicted_curve = CurveJSON(
            freqs_hz=log_freqs.tolist(),
            magnitude_db=predicted_curve_db.tolist(),
        )
        self.peqs = [PEQJSON.from_peq(p) for p in peqs]

    # ------------------------------------------------------------------
    # Apply / reset / verify.
    # ------------------------------------------------------------------

    async def apply(
        self,
        camilla_set_config: Callable[[str], Awaitable[bool]],
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
            emit_correction_config(
                peq_objs,
                out_path=out_path,
                measurement_id=self.session_id,
            )
            self.config_path = out_path
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"YAML emit failed: {e}")
            raise

        try:
            ok = await camilla_set_config(str(out_path))
            if not ok:
                async with self._lock:
                    await self._fail(
                        "CamillaDSP rejected the config (set_config_file_path "
                        "returned False)"
                    )
                return
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"CamillaDSP reload failed: {e}")
            raise

        async with self._lock:
            await self._set_state(
                SessionState.APPLIED,
                config_path=str(out_path),
            )

    async def reset(
        self,
        camilla_set_config: Callable[[str], Awaitable[bool]],
    ) -> None:
        try:
            ok = await camilla_set_config(str(self.cfg.base_config_path))
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
                rolled_back_to=str(self.cfg.base_config_path),
            )

    async def start_verify_sweep(
        self,
        play_sweep_async: Callable[..., Awaitable[Any]],
        *,
        alsa_device: str | None = None,
    ) -> None:
        """One-position re-measurement after Apply. The result lands
        in self.verify_curve / self.verify_metrics — overlaid on the
        chart so the user can see the correction's actual effect."""
        async with self._lock:
            if self.state != SessionState.APPLIED and self.state != SessionState.VERIFIED:
                raise RuntimeError(
                    f"cannot verify from state {self.state.value}"
                )
            await self._set_state(SessionState.VERIFYING)

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
            await play_sweep_async(str(sweep_wav), **kwargs)
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"verify sweep playback failed: {e}")
            raise

        async with self._lock:
            await self._set_state(SessionState.AWAITING_VERIFY_CAPTURE)

    async def on_verify_capture_uploaded(
        self, captured_wav_path: Path,
    ) -> None:
        """Verify capture arrived. Deconv + smooth, store as
        verify_curve, compute deviation metrics. Transition to
        VERIFIED."""
        async with self._lock:
            if self.state != SessionState.AWAITING_VERIFY_CAPTURE:
                raise RuntimeError(
                    f"cannot accept verify capture from state {self.state.value}"
                )
            await self._set_state(SessionState.ANALYZING)
            self.last_capture_path = captured_wav_path

        try:
            log_freqs, log_mag = self._smooth_capture(captured_wav_path)
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"verify analysis failed: {e}")
            raise

        target_db = self._design_target(log_freqs)
        metrics = analysis.deviation_metrics(
            log_mag, target_db, log_freqs,
            f_low=self.cfg.peq_f_low, f_high=self.cfg.peq_f_high,
        )

        self.verify_curve = CurveJSON(
            freqs_hz=log_freqs.tolist(),
            magnitude_db=log_mag.tolist(),
        )
        self.verify_metrics = metrics

        async with self._lock:
            await self._set_state(
                SessionState.VERIFIED,
                rms_db=metrics["rms_db"],
                max_db=metrics["max_db"],
            )

    # ------------------------------------------------------------------
    # Snapshot.
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "state": self.state.value,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "total_positions": self.total_positions,
            "current_position": self.current_position,
            "target_choice": self.target_choice,
            "sweep": (
                self.sweep_meta.to_dict() if self.sweep_meta else None
            ),
            "peqs": [p.__dict__ for p in self.peqs],
            "config_path": (
                str(self.config_path) if self.config_path else None
            ),
            "verify_metrics": self.verify_metrics,
        }
