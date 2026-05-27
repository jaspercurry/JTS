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
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np

from . import (
    analysis,
    browser_audio,
    bundles,
    calibration,
    confidence,
    deconv,
    quality,
    spatial,
    strategy,
    sweep,
)
from .calibration import CalibrationRecord
from .peq import PEQ

logger = logging.getLogger(__name__)


_CORRECTION_FILENAME_RE = re.compile(
    r"^correction_(?P<id>[A-Za-z0-9]+)_(?P<ts>\d+)\.yml$"
)
_SOUND_FILENAME_RE = re.compile(r"^sound_current\.yml$")
_PEQ_KEY_RE = re.compile(r"^\s+(?:peq|room_peq)_\d+:", re.MULTILINE)


def parse_current_correction(
    path: str | None,
    *,
    config_dir: Path = Path("/var/lib/camilladsp/configs"),
) -> dict[str, Any] | None:
    """Describe whatever correction (if any) the given CamillaDSP
    config path represents. Returns None for the base v1.yml or any
    path we don't recognise as a correction emission.

    The filename shape is fixed by `MeasurementSession.apply`:
    ``correction_<session_id>_<unixtime>.yml`` under
    ``/var/lib/camilladsp/configs/``. Anything else (the base
    `/etc/camilladsp/v1.yml`, a hand-edited config, a missing path)
    returns None — the UI treats that as "speaker is flat."
    """
    if not path:
        return None
    p = Path(path)
    if p.parent != Path(config_dir):
        return None
    m = _CORRECTION_FILENAME_RE.match(p.name)
    if not m:
        if not _SOUND_FILENAME_RE.match(p.name):
            return None
        try:
            text = p.read_text()
            peq_count = len(_PEQ_KEY_RE.findall(text))
            applied_at_epoch = int(p.stat().st_mtime)
        except OSError:
            return None
        if peq_count == 0:
            return None
        return {
            "path": str(p),
            "session_id": "sound",
            "applied_at_epoch": applied_at_epoch,
            "peq_count": peq_count,
        }
    try:
        ts = int(m.group("ts"))
    except ValueError:
        return None
    peq_count = 0
    try:
        text = p.read_text()
    except OSError:
        text = ""
    if text:
        peq_count = len(_PEQ_KEY_RE.findall(text))
    return {
        "path": str(p),
        "session_id": m.group("id"),
        "applied_at_epoch": ts,
        "peq_count": peq_count,
    }


def _bundles_enabled() -> bool:
    """Default ON; opt-out via JASPER_CORRECTION_SAVE_BUNDLES=0."""
    return os.environ.get("JASPER_CORRECTION_SAVE_BUNDLES", "1").strip() != "0"


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


class AutolevelStatus(Enum):
    """Auto-level sub-state. Orthogonal to the measurement state
    machine — autolevel can run from any "idle-ish" session state
    (IDLE / READY / APPLIED / VERIFIED / FAILED) without affecting
    the session's measurement flow.
    """
    IDLE = "idle"
    RAMPING = "ramping"
    LOCKED = "locked"
    MAXED_OUT = "maxed_out"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass
class AutolevelData:
    """Tracks one auto-level run. Replaced (not mutated) when the
    user starts a new one.

    `original_main_volume_db` is the CamillaDSP `main_volume` value
    saved at the start of the run, so we can restore it after the
    measurement workflow completes (apply / reset). `current` is
    where the ramp is right now; `locked` is where it ended up
    when the user signalled lock (or where it was when the ramp
    completed without lock). `cap_db` is the dynamic end-of-ramp
    cap computed from `original + bump`; exposed so the client and
    tests can verify what cap the run is operating under.
    """
    status: AutolevelStatus = AutolevelStatus.IDLE
    current_main_volume_db: float = -50.0
    original_main_volume_db: float | None = None
    locked_main_volume_db: float | None = None
    cap_db: float | None = None
    error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        def r(x: float | None) -> float | None:
            return round(x, 2) if x is not None else None
        return {
            "status": self.status.value,
            "current_main_volume_db": r(self.current_main_volume_db),
            "original_main_volume_db": r(self.original_main_volume_db),
            "locked_main_volume_db": r(self.locked_main_volume_db),
            "cap_db": r(self.cap_db),
            "error": self.error,
        }


@dataclass
class SessionConfig:
    sweep_dir: Path = Path("/var/lib/jasper/correction/sweeps")
    capture_dir: Path = Path("/var/lib/jasper/correction/captures")
    sessions_dir: Path = Path("/var/lib/jasper/correction/sessions")
    config_dir: Path = Path("/var/lib/camilladsp/configs")
    base_config_path: Path = Path("/etc/camilladsp/v1.yml")
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
        self.verify_quality: dict[str, Any] | None = None
        self.confidence_report: dict[str, Any] | None = None
        self.position_analysis: dict[str, Any] | None = None

        # Output curves for the chart.
        self.measured_curve: CurveJSON | None = None
        self.target_curve: CurveJSON | None = None
        self.predicted_curve: CurveJSON | None = None
        self.verify_curve: CurveJSON | None = None
        self.verify_metrics: dict[str, float] | None = None
        self.design_report: dict[str, Any] | None = None

        self.peqs: list[PEQJSON] = []
        self.config_path: Path | None = None

        # Sweep cache.
        self.sweep_meta: sweep.SweepMeta | None = None
        self.sweep_wav_path: Path | None = None
        self.last_capture_path: Path | None = None

        # Auto-level sub-state — orthogonal to the measurement
        # state machine; runs against CamillaDSP main_volume +
        # iPhone-mic feedback.
        self.autolevel: AutolevelData = AutolevelData()
        self._autolevel_lock_event: asyncio.Event | None = None
        self._autolevel_cancel_event: asyncio.Event | None = None

        # Optional client-reported room noise floor (the autolevel
        # preflight measures this in the browser before the tone
        # plays). Saved into info.json so debug bundles preserve the
        # context that drove the autolevel target band.
        self.noise_floor_db: float | None = None

        # Snapshot of `current_correction` (path / peq_count / epoch)
        # at the moment `/start` was hit, BEFORE the auto-reset to
        # base config. Lets the bundle reproduce what state the
        # speaker was in when this session began.
        self.current_correction_at_start: dict[str, Any] | None = None

        # Per-session debug bundle. All artifacts (info.json,
        # result.json, per-position WAVs, verify.wav, applied.yml)
        # and mic_calibration.* land here. The directory is created
        # lazily on first write so tests that pass a SessionConfig
        # pointing at a tmp_path don't have to pre-mkdir.
        self.bundle_dir: Path = self.cfg.sessions_dir / self.session_id
        self.save_bundles: bool = _bundles_enabled()

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
        # Bundle artifacts are best-effort — never let a write failure
        # bring down a measurement state transition.
        try:
            self._write_info_json()
        except Exception:  # noqa: BLE001
            logger.exception(
                "bundle info.json write failed (state=%s)", state.value,
            )

    # ------------------------------------------------------------------
    # Bundle artifacts. Each measurement session optionally writes
    # a self-contained debug bundle at sessions/<session_id>/ — info,
    # result curves, per-position captures, applied config copy.
    # ------------------------------------------------------------------

    def _ensure_bundle_dir(self) -> Path | None:
        if not self.save_bundles:
            return None
        try:
            self.bundle_dir.mkdir(parents=True, exist_ok=True)
            (self.bundle_dir / "captures").mkdir(exist_ok=True)
        except OSError as e:
            logger.warning(
                "bundle dir create failed for session %s: %s",
                self.session_id, e,
            )
            return None
        return self.bundle_dir

    def capture_path_for_position(self, idx: int) -> Path:
        """Where a per-position WAV should be written. Falls back to
        cfg.capture_dir when bundles are disabled or the per-session
        dir can't be created — keeps the upload path working even
        when /var/lib/jasper is read-only or full."""
        bundle = self._ensure_bundle_dir()
        if bundle is not None:
            return bundle / "captures" / f"p{idx}.wav"
        self.cfg.capture_dir.mkdir(parents=True, exist_ok=True)
        return self.cfg.capture_dir / (
            f"capture_{self.session_id}_p{idx}_{int(time.time())}.wav"
        )

    def verify_capture_path(self) -> Path:
        """Where the post-Apply re-measurement WAV should land."""
        bundle = self._ensure_bundle_dir()
        if bundle is not None:
            return bundle / "verify.wav"
        self.cfg.capture_dir.mkdir(parents=True, exist_ok=True)
        return self.cfg.capture_dir / (
            f"verify_{self.session_id}_{int(time.time())}.wav"
        )

    def _write_info_json(self) -> None:
        """Atomically rewrite info.json with the current session
        snapshot. Cheap (a few hundred bytes) and called on every
        state transition so a bundle copied off the Pi mid-session
        is always self-describing."""
        bundle = self._ensure_bundle_dir()
        if bundle is None:
            return
        info = {
            "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
            "session_id": self.session_id,
            "state": self.state.value,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "total_positions": self.total_positions,
            "current_position": self.current_position,
            "target_choice": self.target_choice,
            "target_profile": strategy.resolve_target_profile(
                self.target_choice,
            ).to_dict(),
            "strategy_choice": self.strategy_choice,
            "correction_strategy": strategy.resolve_correction_strategy(
                self.strategy_choice,
            ).to_dict(),
            "noise_floor_db": self.noise_floor_db,
            "input_device": self.input_device,
            "mic_calibration": (
                self.mic_calibration.public_metadata()
                if self.mic_calibration
                else None
            ),
            "browser_audio_report": self.browser_audio_report,
            "capture_quality": self.capture_quality,
            "verify_quality": self.verify_quality,
            "confidence_report": self.confidence_report,
            "position_analysis": self.position_analysis,
            "current_correction_at_start": self.current_correction_at_start,
            "autolevel": self.autolevel.snapshot(),
            "sweep_meta": (
                self.sweep_meta.to_dict() if self.sweep_meta else None
            ),
            "peqs": [p.__dict__ for p in self.peqs],
            "design_report": self.design_report,
            "config_path": (
                str(self.config_path) if self.config_path else None
            ),
            "verify_metrics": self.verify_metrics,
            "config": {
                "f1_hz": self.cfg.f1_hz,
                "f2_hz": self.cfg.f2_hz,
                "duration_s": self.cfg.duration_s,
                "sample_rate": self.cfg.sample_rate,
                "amplitude_dbfs": self.cfg.amplitude_dbfs,
                "peq_f_low": self.cfg.peq_f_low,
                "peq_f_high": self.cfg.peq_f_high,
                "peq_max_filters": self.cfg.peq_max_filters,
                "peq_max_cut_db": self.cfg.peq_max_cut_db,
                "peq_max_boost_db": self.cfg.peq_max_boost_db,
                "peq_cuts_only": self.cfg.peq_cuts_only,
                "peq_flatness_target_db": self.cfg.peq_flatness_target_db,
                "correction_strategy": self.cfg.correction_strategy,
            },
        }
        target_path = bundle / "info.json"
        tmp_path = target_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(info, indent=2, default=str))
        tmp_path.replace(target_path)
        self._write_mic_calibration_bundle(bundle)

    def _write_result_json(self) -> None:
        """Snapshot the chart curves + verify after design / verify.
        Result.json is the "what did this measurement actually
        produce" record — separated from info.json so we don't
        rewrite curve data on every state transition."""
        bundle = self._ensure_bundle_dir()
        if bundle is None:
            return
        result = {
            "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
            "session_id": self.session_id,
            "input_device": self.input_device,
            "mic_calibration": (
                self.mic_calibration.public_metadata()
                if self.mic_calibration
                else None
            ),
            "browser_audio_report": self.browser_audio_report,
            "measured": (
                self.measured_curve.__dict__ if self.measured_curve else None
            ),
            "target": (
                self.target_curve.__dict__ if self.target_curve else None
            ),
            "predicted": (
                self.predicted_curve.__dict__ if self.predicted_curve else None
            ),
            "verify": (
                self.verify_curve.__dict__ if self.verify_curve else None
            ),
            "verify_metrics": self.verify_metrics,
            "capture_quality": self.capture_quality,
            "verify_quality": self.verify_quality,
            "confidence_report": self.confidence_report,
            "position_analysis": self.position_analysis,
            "peqs": [p.__dict__ for p in self.peqs],
            "design_report": self.design_report,
        }
        target_path = bundle / "result.json"
        tmp_path = target_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(result, indent=2, default=str))
        tmp_path.replace(target_path)

    def _write_mic_calibration_bundle(self, bundle: Path) -> None:
        """Persist the selected mic calibration into the session bundle.

        `info.json` carries only public metadata. The bundle also needs
        the parsed curve and raw vendor/upload file so a future FIR or
        agent pass can replay the measurement without relying on the
        global `/var/lib/jasper/correction/calibration_mics` registry.
        """
        if self.mic_calibration is None:
            return
        record = self.mic_calibration
        payload = {
            **record.public_metadata(),
            "raw_filename": "mic_calibration.txt",
            "curve": record.curve.to_dict(),
        }
        meta_path = bundle / "mic_calibration.json"
        tmp_meta = meta_path.with_suffix(".json.tmp")
        tmp_meta.write_text(json.dumps(payload, indent=2, default=str))
        tmp_meta.chmod(0o600)
        tmp_meta.replace(meta_path)

        raw_path = Path(record.raw_path)
        if raw_path.exists():
            try:
                bundle_raw = bundle / "mic_calibration.txt"
                shutil.copy2(raw_path, bundle_raw)
                bundle_raw.chmod(0o600)
            except OSError as e:
                logger.warning(
                    "mic_calibration.txt copy failed for session %s: %s",
                    self.session_id, e,
                )

    def _write_position_analysis_json(self) -> None:
        """Persist replayable per-position curves and variance bands.

        `result.json` keeps the chart-level summary. This artifact is
        intentionally more detailed so future FIR / agent passes can
        inspect what each listening position contributed without
        re-running deconvolution.
        """
        bundle = self._ensure_bundle_dir()
        if (
            bundle is None
            or self.position_freqs is None
            or not self.position_magnitudes
            or self.measured_curve is None
        ):
            self.position_analysis = None
            return

        freqs = np.asarray(self.position_freqs, dtype=float)
        spatial_matrix, spatial_error = spatial.build_spatial_matrix(
            self.position_magnitudes,
            freqs,
        )
        if spatial_matrix is None:
            logger.warning(
                "position_analysis unavailable for session %s: %s",
                self.session_id, spatial_error,
            )
            self.position_analysis = None
            return
        std_db = spatial_matrix.std_db
        range_db = spatial_matrix.range_db

        def round_list(values: np.ndarray) -> list[float]:
            return [round(float(v), 3) for v in values]

        variance_summary = (
            (self.confidence_report or {})
            .get("position_variance")
        )
        target_db = (
            np.asarray(self.target_curve.magnitude_db, dtype=float)
            if self.target_curve is not None
            else None
        )
        position_report = confidence.build_position_report(
            position_magnitudes=self.position_magnitudes,
            freqs_hz=freqs,
            measured_db=np.asarray(self.measured_curve.magnitude_db, dtype=float),
            target_db=target_db,
            correction_band_hz=(self.cfg.peq_f_low, self.cfg.peq_f_high),
        )
        payload = {
            "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
            "artifact_schema_version": 1,
            "session_id": self.session_id,
            "correction_band_hz": [self.cfg.peq_f_low, self.cfg.peq_f_high],
            "freqs_hz": round_list(freqs),
            "positions": [
                {
                    "position_index": idx,
                    "magnitude_db": round_list(np.asarray(mag, dtype=float)),
                }
                for idx, mag in enumerate(self.position_magnitudes)
            ],
            "spatial_average_db": [
                round(float(v), 3) for v in self.measured_curve.magnitude_db
            ],
            "variance": {
                "std_db": round_list(std_db),
                "range_db": round_list(range_db),
                "summary": variance_summary,
            },
            "bands": position_report["bands"],
            "feature_flags": position_report["feature_flags"],
        }
        target_path = bundle / "position_analysis.json"
        tmp_path = target_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, default=str))
        tmp_path.replace(target_path)

        self.position_analysis = {
            "artifact_path": "position_analysis.json",
            "artifact_schema_version": 1,
            "position_count": len(self.position_magnitudes),
            "freq_count": int(freqs.shape[0]),
            "variance": variance_summary,
            "bands": position_report["bands"],
            "feature_flags": position_report["feature_flags"],
        }
        if self.design_report is not None:
            self.design_report["position_report"] = {
                "artifact_path": "position_analysis.json",
                "artifact_schema_version": 1,
                "position_count": len(self.position_magnitudes),
                "bands": position_report["bands"],
                "feature_flags": position_report["feature_flags"],
            }

    def _copy_applied_yaml(self) -> None:
        """Copy the just-emitted correction YAML into the bundle. We
        copy rather than symlink so the bundle remains self-contained
        if the user later deletes the file in /var/lib/camilladsp/."""
        bundle = self._ensure_bundle_dir()
        if bundle is None or self.config_path is None:
            return
        try:
            shutil.copy2(self.config_path, bundle / "applied.yml")
        except OSError as e:
            logger.warning(
                "applied.yml copy failed for session %s: %s",
                self.session_id, e,
            )

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
    ) -> tuple[np.ndarray, np.ndarray, quality.CaptureQuality]:
        """Read capture, assess quality, deconvolve, smooth, log-resample.

        Returns (log_freqs, smoothed_db, capture_quality) for both the
        design positions and the verify pass.
        """
        if self.sweep_meta is None:
            raise RuntimeError(
                "no sweep_meta — flow ordering bug (call _ensure_sweep_cache first)"
            )

        captured, sr = sweep.read_wav_mono(captured_wav_path)
        capture_quality = quality.assess_capture(
            captured,
            sample_rate=sr,
            expected_sample_rate=self.cfg.sample_rate,
            sweep_n_samples=self.sweep_meta.n_samples,
            has_mic_calibration=self.mic_calibration is not None,
            input_device=self.input_device,
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
        freqs, mag_db = deconv.magnitude_response(ir, self.cfg.sample_rate)
        smoothed = analysis.smooth_fractional_octave(freqs, mag_db, fraction=48)
        log_freqs, log_mag = analysis.resample_log(freqs, smoothed)
        if self.mic_calibration is not None:
            log_mag = calibration.apply_calibration_curve(
                log_freqs, log_mag, self.mic_calibration.curve,
            )
        log_mag = analysis.normalize_to_band(log_freqs, log_mag)
        return log_freqs, log_mag, capture_quality

    def _quality_report_dict(
        self,
        report: quality.CaptureQuality,
        *,
        capture_kind: str,
        captured_wav_path: Path,
        position_index: int | None = None,
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
        return out

    def _design_target(self, freqs: np.ndarray) -> np.ndarray:
        """Resolve target_choice → dB target curve on `freqs`."""
        return strategy.resolve_target_profile(self.target_choice).curve_db(freqs)

    def _build_confidence_report(self) -> dict[str, Any]:
        return confidence.build_confidence_report(
            total_positions=self.total_positions,
            completed_positions=len(self.position_magnitudes),
            has_mic_calibration=self.mic_calibration is not None,
            input_device=self.input_device,
            capture_quality=self.capture_quality,
            strategy_choice=self.strategy_choice,
            browser_audio_report=self.browser_audio_report,
            position_magnitudes=self.position_magnitudes,
            freqs_hz=self.position_freqs,
            correction_band_hz=(self.cfg.peq_f_low, self.cfg.peq_f_high),
        )

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
            log_freqs, log_mag, capture_quality = self._smooth_capture(
                captured_wav_path,
            )
        except Exception as e:  # noqa: BLE001
            if isinstance(e, quality.CaptureQualityError):
                self.capture_quality.append(self._quality_report_dict(
                    e.report,
                    capture_kind="measurement",
                    captured_wav_path=captured_wav_path,
                    position_index=self.current_position,
                ))
            async with self._lock:
                await self._fail(f"analysis failed: {e}")
            raise

        if self.position_freqs is None:
            self.position_freqs = log_freqs
        self.position_magnitudes.append(log_mag)
        self.capture_quality.append(self._quality_report_dict(
            capture_quality,
            capture_kind="measurement",
            captured_wav_path=captured_wav_path,
            position_index=self.current_position,
        ))
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
        design = strategy.design_correction(
            averaged_db,
            log_freqs,
            target_choice=self.target_choice,
            strategy_choice=self.strategy_choice,
            position_magnitudes=self.position_magnitudes,
        )

        self.measured_curve = CurveJSON(
            freqs_hz=log_freqs.tolist(),
            magnitude_db=averaged_db.tolist(),
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
            from jasper.sound.camilla_yaml import (
                emit_sound_config,
            )
            from jasper.sound.profile import build_sound_filters, load_profile
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"YAML emit failed: {e}")
            raise

        def _prepare_config() -> dict[str, int]:
            profile = load_profile()
            emit_sound_config(
                profile,
                room_peqs=peq_objs,
                out_path=out_path,
                profile_id=self.session_id,
            )
            return {
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
            log_freqs, log_mag, capture_quality = self._smooth_capture(
                captured_wav_path,
            )
        except Exception as e:  # noqa: BLE001
            if isinstance(e, quality.CaptureQualityError):
                self.verify_quality = self._quality_report_dict(
                    e.report,
                    capture_kind="verify",
                    captured_wav_path=captured_wav_path,
                )
            async with self._lock:
                await self._fail(f"verify analysis failed: {e}")
            raise

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
        self.verify_quality = self._quality_report_dict(
            capture_quality,
            capture_kind="verify",
            captured_wav_path=captured_wav_path,
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
        `_autolevel_lock_event` and causes this function to freeze
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
        [-15, -6] dB. So:
          - user at -20 dB → autolevel cap -14 dB (only ~6 dB louder)
          - user at -5 dB  → cap -6 dB (absolute max)
          - user at -45 dB → cap -15 dB (boost to usable measurement
            level)

        Combined with the -12 dBFS tone amplitude (matches the
        sweep), worst-case dongle output at the cap is -18 dBFS —
        far quieter than the prior -6 dBFS tone × -6 dB cap = -12
        dBFS that blasted the user.
        """
        al = self.autolevel = AutolevelData()
        self._autolevel_lock_event = asyncio.Event()
        self._autolevel_cancel_event = asyncio.Event()
        loop = asyncio.get_event_loop()
        tone_task: asyncio.Task | None = None

        async def _graceful_stop(lock_value_db: float | None) -> None:
            """Fade main_volume down to `fade_down_to_db`, then kill
            the tone. Avoids the click that bare proc.kill() would
            produce mid-tone. After the fade, the caller's choice of
            final main_volume (locked / restored) is set."""
            try:
                cur = al.current_main_volume_db
                while cur > fade_down_to_db:
                    cur = max(fade_down_to_db, cur - 2.0)
                    try:
                        await set_main_volume_db(cur)
                    except Exception:  # noqa: BLE001
                        break
                    await asyncio.sleep(fade_step_s)
            finally:
                cancel_tone()
                if tone_task is not None:
                    try:
                        await asyncio.wait_for(tone_task, timeout=2.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
                # After tone is silenced, set the final main_volume.
                if lock_value_db is not None:
                    try:
                        await set_main_volume_db(lock_value_db)
                        al.current_main_volume_db = lock_value_db
                    except Exception:  # noqa: BLE001
                        pass

        try:
            al.original_main_volume_db = float(await get_main_volume_db())
            # Compute the dynamic cap NOW that we know original.
            if end_db is None:
                raw_cap = al.original_main_volume_db + end_db_bump
                end_db = max(end_db_absolute_min, min(raw_cap, end_db_absolute_max))
                logger.info(
                    "autolevel: dynamic end_db=%.1f dB "
                    "(original=%.1f + bump=%.1f, clamped to [%.1f, %.1f])",
                    end_db, al.original_main_volume_db, end_db_bump,
                    end_db_absolute_min, end_db_absolute_max,
                )
            al.cap_db = float(end_db)
            al.status = AutolevelStatus.RAMPING
            logger.info(
                "autolevel: START original_main_volume=%.1f dB "
                "(ramp %.1f → %.1f dB, step=%.1f dB/%.0f ms)",
                al.original_main_volume_db, start_db, end_db,
                step_db, step_interval_s * 1000,
            )

            # CRITICAL: set main_volume to the quiet start BEFORE
            # starting the tone. Without this, the tone briefly plays
            # at the user's previous (often loud) listening level
            # before the ramp drops it — a real "menace" complaint
            # from the first-user test.
            current_db = float(start_db)
            await set_main_volume_db(current_db)
            al.current_main_volume_db = current_db
            # Brief settle so CamillaDSP's volume change reaches the
            # output before any tone arrives.
            await asyncio.sleep(0.1)

            tone_task = asyncio.create_task(play_continuous_tone())

            start_time = loop.time()

            while current_db < end_db:
                # Subdivide each ramp step so lock/cancel can
                # respond within ~10 ms instead of waiting out the
                # full step interval.
                interval_end = loop.time() + step_interval_s
                while loop.time() < interval_end:
                    await asyncio.sleep(0.01)
                    if self._autolevel_lock_event.is_set():
                        al.status = AutolevelStatus.LOCKED
                        al.locked_main_volume_db = current_db
                        logger.info(
                            "autolevel: LOCKED at main_volume=%.1f dB "
                            "(elapsed %.2f s)",
                            current_db, loop.time() - start_time,
                        )
                        await _graceful_stop(current_db)
                        return
                    if self._autolevel_cancel_event.is_set():
                        al.status = AutolevelStatus.CANCELLED
                        logger.info(
                            "autolevel: CANCELLED at main_volume=%.1f dB "
                            "(elapsed %.2f s) — restoring to %.1f dB",
                            current_db, loop.time() - start_time,
                            al.original_main_volume_db,
                        )
                        await _graceful_stop(al.original_main_volume_db)
                        return
                    if loop.time() - start_time > safety_timeout_s:
                        al.status = AutolevelStatus.CANCELLED
                        al.error = (
                            f"safety timeout after {safety_timeout_s}s"
                        )
                        logger.warning(
                            "autolevel: SAFETY TIMEOUT at "
                            "main_volume=%.1f dB — restoring to %.1f dB",
                            current_db, al.original_main_volume_db,
                        )
                        await _graceful_stop(al.original_main_volume_db)
                        return

                current_db = min(end_db, current_db + step_db)
                await set_main_volume_db(current_db)
                al.current_main_volume_db = current_db
                logger.debug(
                    "autolevel: step main_volume=%.1f dB", current_db,
                )

            # Ramp completed without lock.
            al.status = AutolevelStatus.MAXED_OUT
            al.locked_main_volume_db = end_db
            logger.info(
                "autolevel: MAXED_OUT at main_volume=%.1f dB "
                "(software cap) — user must turn up amplifier OR "
                "tap manual Lock button next time",
                end_db,
            )
            # Even on MAXED_OUT, fade gracefully. main_volume stays
            # at end_db (so a manual Lock follow-up would work).
            await _graceful_stop(end_db)
        except Exception as e:  # noqa: BLE001
            al.status = AutolevelStatus.ERROR
            al.error = str(e)
            logger.exception("autolevel failed")
            try:
                if al.original_main_volume_db is not None:
                    await _graceful_stop(al.original_main_volume_db)
                else:
                    cancel_tone()
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._autolevel_lock_event = None
            self._autolevel_cancel_event = None

    async def lock_autolevel(self) -> bool:
        """Signal the running autolevel task to stop ramping and
        lock at the current main_volume. Returns True if a task was
        running."""
        if self._autolevel_lock_event is None:
            return False
        self._autolevel_lock_event.set()
        return True

    async def cancel_autolevel(self) -> bool:
        """Signal the running autolevel task to abort and restore
        the original main_volume."""
        if self._autolevel_cancel_event is None:
            return False
        self._autolevel_cancel_event.set()
        return True

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
            "target_profile": strategy.resolve_target_profile(
                self.target_choice,
            ).to_dict(),
            "strategy_choice": self.strategy_choice,
            "correction_strategy": strategy.resolve_correction_strategy(
                self.strategy_choice,
            ).to_dict(),
            "input_device": self.input_device,
            "mic_calibration": (
                self.mic_calibration.public_metadata()
                if self.mic_calibration
                else None
            ),
            "browser_audio_report": self.browser_audio_report,
            "capture_quality": self.capture_quality,
            "verify_quality": self.verify_quality,
            "confidence_report": self.confidence_report,
            "position_analysis": self.position_analysis,
            "sweep": (
                self.sweep_meta.to_dict() if self.sweep_meta else None
            ),
            "peqs": [p.__dict__ for p in self.peqs],
            "design_report": self.design_report,
            "config_path": (
                str(self.config_path) if self.config_path else None
            ),
            "verify_metrics": self.verify_metrics,
            "autolevel": self.autolevel.snapshot(),
        }
