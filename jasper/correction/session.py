"""Measurement-session state machine.

A single in-memory session tracks the multi-step flow:

    IDLE → PREPARING → SWEEPING → AWAITING_CAPTURE → ANALYZING
         → READY → APPLIED       (or FAILED at any point)

The session owns:
  - the sweep WAV path (cached on disk, deterministic per parameters)
  - the most recent captured WAV (uploaded by the iPhone)
  - the deconvolved IR + smoothed magnitude response
  - the designed PEQ filter set
  - the path of the CamillaDSP YAML config we wrote
  - an event log driving the SSE stream to the browser

Phase 1 is single-position single-session. Multi-position MMM in
Phase 2 will extend this with per-position state and an averaging
step before PEQ design. The shape of `measured_curve_log` /
`target_curve_log` / `peqs` will not change — Phase 2 just feeds
the averaged curve in.
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
from typing import Any

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
    ANALYZING = "analyzing"
    READY = "ready"
    APPLIED = "applied"
    FAILED = "failed"


@dataclass
class CurveJSON:
    """A frequency response in the JSON-serializable shape the
    browser chart consumes — log-spaced grid + dB array."""
    freqs_hz: list[float]
    magnitude_db: list[float]


@dataclass
class PEQJSON:
    """Per-filter PEQ in the JSON shape the browser displays."""
    freq_hz: float
    q: float
    gain_db: float

    @classmethod
    def from_peq(cls, p: PEQ) -> "PEQJSON":
        return cls(freq_hz=p.freq, q=p.q, gain_db=p.gain)


@dataclass
class SessionEvent:
    """Single SSE event payload."""
    seq: int
    timestamp: float          # epoch seconds
    type: str                  # "state" | "log" | "result" | "error"
    payload: dict[str, Any]

    def as_sse_line(self) -> bytes:
        """Render as a complete SSE message (`event:` + `data:` lines
        terminated by a blank line). Browser EventSource consumers
        parse this format natively."""
        body = json.dumps({
            "seq": self.seq,
            "ts": self.timestamp,
            "type": self.type,
            **self.payload,
        })
        return f"event: {self.type}\ndata: {body}\n\n".encode("utf-8")


@dataclass
class SessionConfig:
    """Per-session paths and parameters. Defaulted from environment
    so tests can override without touching the env."""
    # Filesystem paths. /var/lib/camilladsp is owned by camilla and
    # already exists; we add a `configs` subdir at install time.
    sweep_dir: Path = Path("/var/lib/jasper/correction/sweeps")
    capture_dir: Path = Path("/var/lib/jasper/correction/captures")
    config_dir: Path = Path("/var/lib/camilladsp/configs")
    base_config_path: Path = Path("/etc/camilladsp/v1.yml")

    # Sweep parameters. Overrideable for hardware experiments.
    f1_hz: float = 20.0
    f2_hz: float = 20000.0
    duration_s: float = 10.0
    sample_rate: int = 48000
    amplitude_dbfs: float = -12.0

    # Filter design parameters. Match Jasper's known-good REW workflow.
    peq_f_low: float = 20.0
    peq_f_high: float = 350.0
    peq_max_filters: int = 5
    peq_max_cut_db: float = -10.0
    peq_max_boost_db: float = 3.0
    peq_cuts_only: bool = True
    peq_flatness_target_db: float = 1.0


class MeasurementSession:
    """One round of measure → analyze → apply. Lives in memory; not
    persisted (the user can always re-measure if the daemon
    restarts).

    Thread-safety: all transitions go through async methods called
    from the FastAPI-style HTTP handler thread, which serializes
    them with an asyncio.Lock. The SSE event log is appended in the
    same lock, so the browser sees a consistent ordering.
    """

    def __init__(self, cfg: SessionConfig | None = None) -> None:
        self.cfg = cfg or SessionConfig()
        self.session_id = uuid.uuid4().hex[:12]
        self.state = SessionState.IDLE
        self.started_at = time.time()
        self.updated_at = self.started_at
        self.error: str | None = None
        # Inputs / intermediate artifacts.
        self.sweep_meta: sweep.SweepMeta | None = None
        self.sweep_wav_path: Path | None = None
        self.captured_wav_path: Path | None = None
        # Outputs the browser cares about.
        self.measured_curve: CurveJSON | None = None
        self.target_curve: CurveJSON | None = None
        self.predicted_curve: CurveJSON | None = None
        self.peqs: list[PEQJSON] = []
        self.config_path: Path | None = None
        # Event log for SSE.
        self._events: list[SessionEvent] = []
        self._event_seq = 0
        self._lock = asyncio.Lock()
        # Subscribers (asyncio.Queue per /events listener) are pushed
        # to as events are appended. The set is mutated under _lock.
        self._subscribers: set[asyncio.Queue] = set()

    # ------------------------------------------------------------------
    # Event log + subscription.
    # ------------------------------------------------------------------

    def _emit(self, type_: str, payload: dict[str, Any]) -> None:
        """Append an event and fan out to subscribers. Caller must
        hold self._lock."""
        self._event_seq += 1
        ev = SessionEvent(
            seq=self._event_seq,
            timestamp=time.time(),
            type=type_,
            payload=payload,
        )
        self._events.append(ev)
        self.updated_at = ev.timestamp
        for q in list(self._subscribers):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                # Slow consumer — drop the oldest event from their
                # queue and try again. Better than blocking the
                # producer.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(ev)
                except asyncio.QueueFull:
                    pass

    async def subscribe(self) -> asyncio.Queue:
        """Add a subscriber. Caller drains the queue and renders to
        SSE. The subscriber sees ALL prior events first (so a
        late-joiner gets context), then live ones."""
        q: asyncio.Queue = asyncio.Queue(maxsize=128)
        async with self._lock:
            for ev in self._events:
                try:
                    q.put_nowait(ev)
                except asyncio.QueueFull:
                    break
            self._subscribers.add(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers.discard(q)

    # ------------------------------------------------------------------
    # State transitions.
    # ------------------------------------------------------------------

    async def _set_state(self, state: SessionState, **extra: Any) -> None:
        """Transition. Caller must hold self._lock."""
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
        """Move to FAILED with an error message. Caller must hold
        self._lock."""
        self.error = message
        self.state = SessionState.FAILED
        self._emit("error", {"message": message})
        logger.error("session %s failed: %s", self.session_id, message)

    # ------------------------------------------------------------------
    # Phase 1 flow.
    # ------------------------------------------------------------------

    async def prepare_and_play_sweep(
        self,
        play_sweep_async: Any,  # callable: (path, alsa_device) -> awaitable
        *,
        alsa_device: str | None = None,
    ) -> None:
        """PREPARING → SWEEPING → AWAITING_CAPTURE.

        Called by the HTTP handler after wrapping in a
        measurement_window() context. We assume the caller has
        already paused renderers + voice_daemon — this method just
        plays the sweep and signals ready-for-upload.
        """
        async with self._lock:
            if self.state not in (SessionState.IDLE, SessionState.READY,
                                   SessionState.APPLIED, SessionState.FAILED):
                raise RuntimeError(
                    f"cannot start sweep from state {self.state}"
                )
            await self._set_state(SessionState.PREPARING)

        # Generate (or fetch from cache) the sweep WAV.
        try:
            self.cfg.sweep_dir.mkdir(parents=True, exist_ok=True)
            sweep_wav = self.cfg.sweep_dir / (
                f"sweep_{int(self.cfg.f1_hz)}_{int(self.cfg.f2_hz)}_"
                f"{int(self.cfg.duration_s * 1000)}ms_"
                f"{self.cfg.sample_rate}Hz_"
                f"{int(abs(self.cfg.amplitude_dbfs) * 10)}dbm.wav"
            )
            sweep_signal, meta = sweep.synchronized_swept_sine(
                f1=self.cfg.f1_hz,
                f2=self.cfg.f2_hz,
                duration_approx_s=self.cfg.duration_s,
                sample_rate=self.cfg.sample_rate,
                amplitude_dbfs=self.cfg.amplitude_dbfs,
            )
            sweep.write_sweep_wav(sweep_wav, sweep_signal, self.cfg.sample_rate)
            self.sweep_meta = meta
            self.sweep_wav_path = sweep_wav
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"sweep generation failed: {e}")
            raise

        async with self._lock:
            await self._set_state(SessionState.SWEEPING, duration_s=meta.duration_s)

        try:
            kwargs = {"alsa_device": alsa_device} if alsa_device else {}
            await play_sweep_async(str(sweep_wav), **kwargs)
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"sweep playback failed: {e}")
            raise

        async with self._lock:
            await self._set_state(SessionState.AWAITING_CAPTURE)

    async def on_capture_uploaded(
        self, captured_wav_path: Path,
    ) -> None:
        """AWAITING_CAPTURE → ANALYZING → READY.

        Runs the deconv → smooth → PEQ-design pipeline. The chart
        data ends up in self.measured_curve / self.target_curve /
        self.predicted_curve and is emitted as a `result` event for
        the browser to render.
        """
        async with self._lock:
            if self.state != SessionState.AWAITING_CAPTURE:
                raise RuntimeError(
                    f"cannot accept capture from state {self.state}"
                )
            await self._set_state(SessionState.ANALYZING)
            self.captured_wav_path = captured_wav_path

        try:
            self._run_analysis_pipeline()
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"analysis failed: {e}")
            raise

        async with self._lock:
            await self._set_state(
                SessionState.READY,
                peq_count=len(self.peqs),
            )
            # Emit the chart data for the browser.
            self._emit(
                "result",
                {
                    "measured": (
                        self.measured_curve.__dict__
                        if self.measured_curve else None
                    ),
                    "target": (
                        self.target_curve.__dict__
                        if self.target_curve else None
                    ),
                    "predicted": (
                        self.predicted_curve.__dict__
                        if self.predicted_curve else None
                    ),
                    "peqs": [p.__dict__ for p in self.peqs],
                },
            )

    def _run_analysis_pipeline(self) -> None:
        """Pure(ish) pipeline: read captured WAV → deconv → smooth →
        log-resample → design PEQ → predicted response. Fills the
        instance state. Synchronous (CPU-bound numpy / scipy work);
        the caller already moved us to ANALYZING under the lock.
        """
        if self.captured_wav_path is None or self.sweep_meta is None:
            raise RuntimeError(
                "missing captured_wav_path or sweep_meta — flow ordering bug"
            )

        captured, sr = sweep.read_wav_mono(self.captured_wav_path)
        if sr != self.cfg.sample_rate:
            # The Phase 0 page pins iOS to 48 kHz. If we land here,
            # something failed the verify step earlier and the browser
            # uploaded at the wrong rate. Bail loudly.
            raise ValueError(
                f"captured sample rate {sr} != expected {self.cfg.sample_rate}; "
                f"the iOS Safari verify step should have caught this"
            )

        # Regenerate the sweep signal in memory (faster than reading
        # the WAV back) using the same parameters that wrote the
        # cached file. The signal is deterministic from
        # synchronized_swept_sine's parameters.
        sweep_signal, _ = sweep.synchronized_swept_sine(
            f1=self.sweep_meta.f1,
            f2=self.sweep_meta.f2,
            duration_approx_s=self.sweep_meta.duration_s,
            sample_rate=self.sweep_meta.sample_rate,
            amplitude_dbfs=self.sweep_meta.amplitude_dbfs,
        )
        ir = deconv.deconvolve(
            captured.astype(np.float64), sweep_signal.astype(np.float64),
            sample_rate=self.cfg.sample_rate,
        )

        freqs, mag_db = deconv.magnitude_response(ir, self.cfg.sample_rate)
        smoothed = analysis.smooth_fractional_octave(freqs, mag_db, fraction=48)
        log_freqs, log_mag = analysis.resample_log(freqs, smoothed)
        log_mag = analysis.normalize_to_band(log_freqs, log_mag)

        target_db = target.flat_target(log_freqs)

        peqs = peq.design_peq(
            log_mag, target_db, log_freqs,
            f_low=self.cfg.peq_f_low,
            f_high=self.cfg.peq_f_high,
            max_filters=self.cfg.peq_max_filters,
            max_cut_db=self.cfg.peq_max_cut_db,
            max_boost_db=self.cfg.peq_max_boost_db,
            cuts_only=self.cfg.peq_cuts_only,
            flatness_target_db=self.cfg.peq_flatness_target_db,
        )
        predicted_shift = peq.predicted_response(peqs, log_freqs)
        predicted_curve = log_mag + predicted_shift

        self.measured_curve = CurveJSON(
            freqs_hz=log_freqs.tolist(),
            magnitude_db=log_mag.tolist(),
        )
        self.target_curve = CurveJSON(
            freqs_hz=log_freqs.tolist(),
            magnitude_db=target_db.tolist(),
        )
        self.predicted_curve = CurveJSON(
            freqs_hz=log_freqs.tolist(),
            magnitude_db=predicted_curve.tolist(),
        )
        self.peqs = [PEQJSON.from_peq(p) for p in peqs]

    async def apply(self, camilla_set_config: Any) -> None:
        """READY → APPLIED.

        Writes the YAML and calls camilla.set_config_file_path. The
        callable signature is `async (path: str) -> bool` so we can
        pass a bound method or a test stub.
        """
        async with self._lock:
            if self.state != SessionState.READY:
                raise RuntimeError(
                    f"cannot apply from state {self.state}"
                )

        # Build YAML and write to /var/lib/camilladsp/configs/...
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

    async def reset(self, camilla_set_config: Any) -> None:
        """APPLIED → IDLE: roll back to the as-shipped base config.

        Equivalent to "remove all correction filters". The user's
        listening_level / main_volume is preserved (those are
        controlled by VolumeCoordinator, not by the YAML reload).
        """
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

    # ------------------------------------------------------------------
    # Snapshot.
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """JSON-serializable summary for GET /status. Live-readable
        without taking the lock — fields are atomically updated and
        a torn read at worst shows the previous state."""
        return {
            "session_id": self.session_id,
            "state": self.state.value,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "sweep": (
                self.sweep_meta.to_dict() if self.sweep_meta else None
            ),
            "peqs": [p.__dict__ for p in self.peqs],
            "config_path": (
                str(self.config_path) if self.config_path else None
            ),
        }
